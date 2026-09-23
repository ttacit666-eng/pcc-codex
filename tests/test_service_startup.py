"""Startup failures cannot dispatch queued work or leave a new owner lock."""
import asyncio
from contextlib import asynccontextmanager
import socket
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock,MagicMock
import pytest
import uvicorn
from starlette.applications import Starlette
from pcc import server


@pytest.fixture
def setup_service(tmp_path, monkeypatch):
    root=tmp_path/'state';root.mkdir()
    controller=MagicMock(root=root)
    app=Mock(settings=SimpleNamespace(host='127.0.0.1',port=8876,log_level='INFO'))
    transport=Mock()
    ready={}
    def make_transport(config,on_ready):
        ready.update(config=config,callback=on_ready)
        return transport
    monkeypatch.setattr(server,'load',lambda _: {'deployment_enabled':True})
    monkeypatch.setattr(server,'preflight_runtime',lambda: {})
    monkeypatch.setattr(server,'Controller',lambda: controller)
    monkeypatch.setattr(server,'build',lambda *_: app)
    monkeypatch.setattr(server,'_WorkerReadyServer',make_transport)
    return root,controller,app,transport,ready


def test_invalid_runtime_does_not_create_lock_or_worker(setup_service,monkeypatch):
    root,controller,app,transport,ready=setup_service
    def fail():raise FileNotFoundError('PCC_CLI_MISSING')
    monkeypatch.setattr(server,'preflight_runtime',fail)
    with pytest.raises(FileNotFoundError):server.serve('unused')
    assert not (root/'service.lock').exists()
    controller.reconcile.assert_not_called()
    controller.execute.assert_not_called()
    transport.run.assert_not_called()


def test_reconcile_failure_cleans_only_new_lock(setup_service):
    root,controller,app,transport,ready=setup_service
    controller.reconcile.side_effect=RuntimeError('synthetic reconciliation failure')
    with pytest.raises(RuntimeError):server.serve('unused')
    assert not (root/'service.lock').exists()
    controller.execute.assert_not_called()
    transport.run.assert_not_called()


def test_existing_owner_lock_never_reclaimed(setup_service):
    root,controller,app,transport,ready=setup_service
    (root/'service.lock').write_text('987654')
    with pytest.raises(FileExistsError):server.serve('unused')
    assert (root/'service.lock').read_text()=='987654'
    controller.reconcile.assert_not_called()
    controller.execute.assert_not_called()
    transport.run.assert_not_called()


def test_delayed_transport_failure_never_dispatches_and_releases_own_lock(setup_service):
    root,controller,app,transport,ready=setup_service
    controller.db.return_value.__enter__.return_value.execute.return_value.fetchone.return_value={'id':'synthetic-queued'}
    def delayed_failure():
        time.sleep(.8)  # Longer than the worker's polling interval.
        raise OSError('synthetic delayed transport bind failure')
    transport.run.side_effect=delayed_failure
    with pytest.raises(OSError):server.serve('unused')
    assert not (root/'service.lock').exists()
    controller.execute.assert_not_called()


def test_ready_callback_dispatches_and_preserves_streamable_app(setup_service):
    root,controller,app,transport,ready=setup_service
    executed=threading.Event()
    controller.db.return_value.__enter__.return_value.execute.return_value.fetchone.return_value={'id':'synthetic-queued'}
    controller.execute.side_effect=lambda _:executed.set()
    def run_ready():
        assert not executed.is_set()
        ready['callback']()
        assert executed.wait(2)
    transport.run.side_effect=run_ready
    server.serve('unused')
    assert ready['config'].app is app.streamable_http_app.return_value
    assert (ready['config'].host,ready['config'].port)==('127.0.0.1',8876)
    controller.execute.assert_called_once_with('synthetic-queued')
    assert not (root/'service.lock').exists()


def test_shutdown_keeps_owner_lock_for_active_executor(setup_service):
    root,controller,app,transport,ready=setup_service
    entered=threading.Event();release=threading.Event();finished=threading.Event()
    controller.db.return_value.__enter__.return_value.execute.return_value.fetchone.return_value={'id':'synthetic-queued'}
    def execute(_):
        entered.set()
        release.wait(10)
        finished.set()
    controller.execute.side_effect=execute
    def run_ready():
        ready['callback']()
        assert entered.wait(2)
    transport.run.side_effect=run_ready
    try:
        server.serve('unused')
        assert (root/'service.lock').exists()
    finally:
        release.set()
        assert finished.wait(2)


def test_uvicorn_real_bound_socket_precedes_ready_callback():
    async def go():
        observed=[]
        def on_ready():
            assert http.started
            observed.append(http.servers[0].sockets[0].getsockname())
        config=uvicorn.Config(Starlette(),host='127.0.0.1',port=0,log_level='error')
        http=server._WorkerReadyServer(config,on_ready)
        config.load();http.lifespan=config.lifespan_class(config)
        try:
            await http.startup()
            assert len(observed)==1
            reader,writer=await asyncio.open_connection(*observed[0][:2])
            writer.close();await writer.wait_closed()
        finally:
            await http.shutdown()
    asyncio.run(go())


def test_uvicorn_real_delayed_bind_failure_never_calls_ready():
    async def go(port):
        @asynccontextmanager
        async def delayed_lifespan(_):
            await asyncio.sleep(.8)
            yield
        callback=Mock()
        config=uvicorn.Config(Starlette(lifespan=delayed_lifespan),host='127.0.0.1',port=port,log_level='error')
        http=server._WorkerReadyServer(config,callback)
        config.load();http.lifespan=config.lifespan_class(config)
        with pytest.raises(SystemExit):await http.startup()
        assert http.started is False
        callback.assert_not_called()
    with socket.socket() as occupied:
        occupied.bind(('127.0.0.1',0));occupied.listen()
        asyncio.run(go(occupied.getsockname()[1]))
