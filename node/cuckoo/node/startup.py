# Copyright (C) 2019-2021 Estonian Information System Authority.
# See the file 'LICENSE' for copying permission.

import os
import time
from threading import Thread

from cuckoo.common import config, shutdown
from cuckoo.common.log import CuckooGlobalLogger
from cuckoo.common.packages import enumerate_plugins
from cuckoo.common.startup import StartupError
from cuckoo.common.startup import load_configurations
from cuckoo.common.storage import Paths, UnixSocketPaths, cuckoocwd

from cuckoo.node.node import Node

log = CuckooGlobalLogger(__name__)


def start_taskrunner(nodectx):
    from cuckoo.node.taskrunner import TaskRunner
    from multiprocessing import Process

    sockpath = UnixSocketPaths.task_runner()
    if sockpath.exists():
        raise StartupError(f"Task runner socket path already exists: {sockpath}")

    taskrunner = TaskRunner(sockpath, cuckoocwd, loglevel=nodectx.loglevel)
    runner_proc = Process(target=taskrunner.start)

    def _taskrunner_stopper():
        runner_proc.terminate()
        runner_proc.join(timeout=30)

    # This should be stopped as one of the first components. This way, tasks
    # that are stopped during a run can still more cleanly stop.
    shutdown.register_shutdown(_taskrunner_stopper, order=2)
    runner_proc.start()

    waited = 0
    MAXWAIT = 5
    while not os.path.exists(sockpath):
        if waited >= MAXWAIT:
            raise StartupError(f"Task runner was not started after {MAXWAIT} seconds.")

        if not runner_proc.is_alive():
            raise StartupError("Task runner stopped unexpectedly")
        waited += 0.5
        time.sleep(0.5)


def get_rooter_routes(nodectx):
    from pathlib import Path
    from cuckoo.common.route import Routes
    from cuckoo.common.clients import RooterClient, ActionFailedError

    if not config.cfg("cuckoo.yaml", "network_routing", "enabled"):
        nodectx.routes = Routes(available=[])
        return

    sock_path = Path(config.cfg("cuckoo.yaml", "network_routing", "rooter_socket"))
    if not sock_path.exists():
        raise StartupError(
            f"Network routing is enabled, but rooter socket path does not"
            f" exist: {sock_path}"
        )

    try:
        nodectx.rooter_sock = str(sock_path)
        nodectx.routes = RooterClient.get_routes(sock_path, timeout=60)
    except ActionFailedError as e:
        raise StartupError(
            f"Failure while asking rooter for available routes. Error: {e}"
        )


def start_nodestatecontrol(nodectx, threaded=False):
    from cuckoo.node.control import NodeTaskController

    sockpath = UnixSocketPaths.node_state_controller()
    if sockpath.exists():
        raise StartupError(
            f"Failed to start state controller: "
            f"Unix socket path already exists: {sockpath}"
        )

    state_controller = NodeTaskController(sockpath, nodectx)
    nodectx.state_controller = state_controller
    shutdown.register_shutdown(state_controller.stop)

    if threaded:
        state_th = Thread(target=state_controller.start)
        state_th.start()
    else:
        state_controller.start()


def start_machinerymanager(nodectx):
    from cuckoo.common.machines import read_machines_dump
    from cuckoo.machineries.abstracts import Machinery
    from cuckoo.node.machinery import MachineryManager, MachineryManagerError

    all_machineries = enumerate_plugins(
        "cuckoo.machineries.modules", globals(), Machinery
    )
    enabled = config.cfg("cuckoo", "machineries")

    machinery_classes = []
    for machinery_class in all_machineries:
        if not machinery_class.name:
            continue

        if machinery_class.name.lower() in enabled:
            machinery_classes.append(machinery_class)

    # Load the machine states file if it is present.
    machine_states = {}
    dump_path = Paths.machinestates()
    if os.path.isfile(dump_path):
        machine_states = read_machines_dump(dump_path)

    sockpath = UnixSocketPaths.machinery_manager()
    if sockpath.exists():
        raise StartupError(f"Machinery manager socket path already exists: {sockpath}")

    manager = MachineryManager(sockpath, nodectx)
    nodectx.machinery_manager = manager
    shutdown.register_shutdown(manager.stop)
    shutdown.register_shutdown(manager.shutdown_all, order=999)

    try:
        manager.load_machineries(machinery_classes, previous_machinelist=machine_states)
    except MachineryManagerError as e:
        raise StartupError(f"Machinery loading failure: {e}")

    # Register the machinery stopping method as one that must be called last
    # to ensure any machines started during shutdown is still stopped.
    manager_th = Thread(target=manager.start)
    manager_th.start()


def start_resultserver(nodectx):
    from cuckoo.node.resultserver import ResultServer, servers
    from multiprocessing import Process

    sockpath = UnixSocketPaths.result_server()
    if sockpath.exists():
        raise StartupError(
            "Resultserver unix socket already/still exists. "
            "Remove it there is no other Cuckoo instance running"
            f" using the specified Cuckoo CWD. {sockpath}"
        )

    ip = config.cfg("cuckoo", "resultserver", "listen_ip")
    port = config.cfg("cuckoo", "resultserver", "listen_port")
    rs = ResultServer(sockpath, cuckoocwd, ip, port, loglevel=nodectx.loglevel)
    log.debug(
        "Starting resultserver.",
        listenip=ip,
        listenport=port,
        sockpath=sockpath,
        cwd=cuckoocwd.root,
    )
    rs_proc = Process(target=rs.start)

    def _rs_stopper():
        rs_proc.terminate()

    shutdown.register_shutdown(_rs_stopper)
    rs_proc.start()

    waited = 0
    MAXWAIT = 5
    while not sockpath.exists():
        if waited >= MAXWAIT:
            raise StartupError(f"Resultserver was not started after {MAXWAIT} seconds.")

        if not rs_proc.is_alive():
            raise StartupError("Resultserver stopped unexpectedly")
        waited += 0.5
        time.sleep(0.5)

    log.debug("Resultserver process started.", pid=rs_proc.pid)

    servers.add(sockpath, ip, port)


class NodeCtx:
    def __init__(self):
        self.node = None
        self.loglevel = None
        self.machinery_manager = None
        self.state_controller = None
        self.routes = None
        self.zip_results = False
        self.rooter_sock = None
        self.is_resetting = False
        # Live view components (optional)
        self.live_broker = None
        self.vnc_token_manager = None
        self.websockify_process = None


def start_live_components(ctx, api_loop=None):
    """Initialize optional live telemetry and VNC proxy components.

    This function is best-effort: if websockify is missing or VNC is not
    configured, the rest of Cuckoo keeps working normally.
    """
    from cuckoo.node.live import LiveEventBroker
    from cuckoo.node.vncproxy import VNCTokenManager, WebsockifyProcess
    from cuckoo.node.resultserver import FileUpload, ScreenshotUpload

    broker = LiveEventBroker()
    if api_loop:
        broker.attach_loop(api_loop)

    ctx.live_broker = broker

    # Inject broker into the result server protocol handlers so they can
    # stream events during file uploads.  These are class-level attributes
    # so they work across all handler instances.
    FileUpload.live_broker = broker
    ScreenshotUpload.live_broker = broker

    # VNC token file and websockify (optional, only if port is configured)
    try:
        vnc_ws_port = config.cfg("cuckoo.yaml", "cuckoo", "live", "vnc_ws_port")
    except Exception:
        vnc_ws_port = 0

    if vnc_ws_port > 0:
        token_file = str(Paths.log("vnc_tokens.conf"))
        vnc_mgr = VNCTokenManager(token_file)
        ctx.vnc_token_manager = vnc_mgr

        ws_proc = WebsockifyProcess(vnc_ws_port, token_file)
        ctx.websockify_process = ws_proc
        ws_proc.start()
        shutdown.register_shutdown(ws_proc.stop)
    else:
        log.debug(
            "Live VNC proxy not started (cuckoo.live.vnc_ws_port not set or 0)"
        )


def start_local(stream_receiver, loglevel, api_host="localhost", api_port=8090):
    from cuckoo.node.webapi import make_api_runner

    ctx = NodeCtx()
    ctx.loglevel = loglevel
    # Results should not be zipped if it is a local node.
    ctx.zip_results = False
    get_rooter_routes(ctx)
    start_resultserver(ctx)
    start_machinerymanager(ctx)
    start_taskrunner(ctx)

    runner = make_api_runner(ctx)
    shutdown.register_shutdown(runner.stop)
    node = Node(ctx, stream_receiver)
    ctx.node = node
    shutdown.register_shutdown(node.stop)
    node.start()

    try:
        runner.create_site(host=api_host, port=api_port)
    except OSError as e:
        raise StartupError(e)

    start_live_components(ctx, api_loop=runner.loop)
    start_nodestatecontrol(ctx, threaded=True)

    # Run the aiohttp event loop in a background thread so the caller
    # (main scheduler) can continue in the foreground.
    api_thread = Thread(target=runner.run_forever, daemon=True, name="node-webapi")
    api_thread.start()

    return ctx


def start_remote(loglevel, api_host="localhost", api_port=8090):
    from cuckoo.node.webapi import make_api_runner
    from multiprocessing import set_start_method
    from cuckoo.common.startup import init_global_logging
    import threading

    set_start_method("spawn")
    cuckoocwd.set(cuckoocwd.DEFAULT, analyses_dir="nodework")

    init_global_logging(loglevel, Paths.log("node.log"))
    try:
        load_configurations()
        config.load_config(Paths.config("distributed.yaml"))
    except config.MissingConfigurationFileError as e:
        raise StartupError(f"Missing configuration file: {e}")
    except config.ConfigurationError as e:
        raise StartupError(e)

    ctx = NodeCtx()
    ctx.loglevel = loglevel
    # Results should be zipped after a task finished
    ctx.zip_results = True
    get_rooter_routes(ctx)
    start_resultserver(ctx)
    start_machinerymanager(ctx)
    start_taskrunner(ctx)

    runner = make_api_runner(ctx)
    shutdown.register_shutdown(runner.stop)
    node = Node(ctx, runner.statesse)
    shutdown.register_shutdown(node.stop)

    ctx.node = node
    node.start()

    try:
        runner.create_site(host=api_host, port=api_port)
    except OSError as e:
        raise StartupError(e)

    # Attach the live broker to the api event loop so it can dispatch events
    start_live_components(ctx, api_loop=runner.loop)

    threading.Thread(target=runner.run_forever).start()
    start_nodestatecontrol(ctx)
