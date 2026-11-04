#!/bin/python3

# The worker's part of registration without passwords (ramani plans/nyayo-2.md, Worker registration): hello and
# the token kept beside the client, the describe job and "wait", a client update that keeps the token, and fastchess
# as a binary from the server. Run from UnitTests/ with the Client's requirements installed.

import hashlib
import io
import os
import stat
import sys
import tarfile
import types

import pytest

PARENT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.path.pardir))
sys.path.insert(0, os.path.join(PARENT, 'Client'))


class Reply:
    def __init__(self, body=None, content=b'', status=200):
        self.body, self.content, self.status_code = body or {}, content, status
    def json(self): return self.body


@pytest.fixture
def token_file(monkeypatch, tmp_path):
    import client
    monkeypatch.setattr(client, 'TOKEN_FILE', str(tmp_path / 'worker.token'))
    return tmp_path / 'worker.token'


def config(**fields):
    base = dict(server='http://x', identity='alex-pc', join=None, token=None, toolchains={}, last_wait=None,
                machine_id=None, secret_token=None, cxx_comp='g++', blacklist={})
    return types.SimpleNamespace(**{ **base, **fields })


def test_hello_keeps_the_token_beside_the_client(monkeypatch, token_file):
    import worker

    sent = {}
    monkeypatch.setattr(worker.requests, 'post', lambda target, **kw: sent.update(target=target, **kw) or Reply({ 'token' : 'abc' }))

    cfg = config(join='ONE-TIME')
    worker.server_hello(cfg)

    assert sent['target'].endswith('clientHello/')
    assert sent['data']['name'] == 'alex-pc' and sent['data']['join'] == 'ONE-TIME'
    assert sent['data']['client_ver'] == worker.CLIENT_VERSION
    assert 'username' not in sent['data'] and 'password' not in sent['data'] and 'token' not in sent['data']
    assert cfg.token == 'abc' and token_file.read_text() == 'abc'
    if os.name != 'nt':
        assert stat.S_IMODE(token_file.stat().st_mode) == 0o600

    # A restart reads it again, and sends it along
    import client
    cfg = config(token=client.read_token())
    worker.server_hello(cfg)
    assert sent['data']['token'] == 'abc'


def test_a_refused_hello_ends_the_worker(monkeypatch, token_file):
    import worker

    monkeypatch.setattr(worker.requests, 'post', lambda *a, **kw: Reply({ 'error' : 'Bad join code' }))
    with pytest.raises(SystemExit):
        worker.server_hello(config(join='WRONG'))
    assert not token_file.exists()


def test_every_call_carries_the_token(monkeypatch):
    import worker

    sent = {}
    monkeypatch.setattr(worker.requests, 'post', lambda target, **kw: sent.update(kw) or Reply({}))
    worker.ServerReporter.report(config(token='abc'), 'clientHeartbeat', {})
    assert sent['data']['token'] == 'abc'


def test_the_describe_job_reports_the_tools_and_asks_again(monkeypatch, capsys):
    import worker

    versions = { 'g++' : '13.2.0', 'make' : '4.3' }
    def get_version(tool):
        if tool in versions: return versions[tool]
        raise Exception('missing')
    registered = []
    monkeypatch.setattr(worker, 'get_version', get_version)
    monkeypatch.setattr(worker, 'server_register_system', lambda cfg: registered.append(dict(cfg.toolchains)))
    monkeypatch.setattr(worker.requests, 'post', lambda *a, **kw: Reply({ 'describe' : ['g++', 'make', 'dotnet'] }))

    cfg = config(workload='left over')
    worker.server_request_workload(cfg)

    assert registered == [{ 'g++' : '13.2.0', 'make' : '4.3', 'dotnet' : None }]
    assert cfg.workload is None


def test_wait_prints_its_reason_once_and_polls_on(monkeypatch, capsys):
    import worker

    monkeypatch.setattr(worker.requests, 'post', lambda *a, **kw: Reply({ 'wait' : 'waiting for approval' }))
    cfg = config(workload='left over')
    for _ in range(3):
        worker.server_request_workload(cfg)

    assert cfg.workload is None
    assert capsys.readouterr().out.count('waiting for approval') == 1


def test_a_client_update_keeps_the_token(tmp_path):
    import client

    extracted, installed = tmp_path / 'Client', tmp_path / 'installed'
    extracted.mkdir(); installed.mkdir()
    (extracted / 'client.py').write_text('new client')
    (extracted / 'worker.py').write_text('new worker')
    (extracted / 'worker.token').write_text('from the archive')
    (installed / 'client.py').write_text('running client')
    (installed / 'worker.py').write_text('old worker')
    (installed / 'worker.token').write_text('mine')

    assert client.copy_client_files(str(extracted), str(installed)) == 1
    assert (installed / 'worker.token').read_text() == 'mine'
    assert (installed / 'client.py').read_text() == 'running client'
    assert (installed / 'worker.py').read_text() == 'new worker'


def test_the_client_takes_no_credentials(monkeypatch):
    import client

    monkeypatch.setattr(sys, 'argv', ['client.py', '-S', 'http://x', '--join', 'CODE', '-T', '4'])
    args = client.parse_arguments()
    assert args.server == 'http://x' and args.join == 'CODE'
    assert not hasattr(args, 'username') and not hasattr(args, 'password')


def test_a_missing_compiler_is_reported_not_fatal(monkeypatch, capsys):
    import worker

    monkeypatch.setattr(worker, 'locate_utility', lambda *a, **kw: None)
    cfg = types.SimpleNamespace()
    worker.Configuration.check_requirements(cfg)
    assert cfg.cxx_comp is None
    assert 'missing' in capsys.readouterr().out


def runner_data(content, url='fastchess-linux-x86-64.tar', sha=None):
    return { 'binary' : { 'url' : url, 'sha256' : sha or hashlib.sha256(content).hexdigest() }, 'fastchess_min_version' : '1.0.0' }


def tar_with(name, content):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w') as archive:
        info = tarfile.TarInfo('fastchess-linux-x86-64/' + name)
        info.size, info.mode = len(content), 0o755
        archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


@pytest.mark.skipif(os.name == 'nt', reason='the stand-in runner is a shell script')
def test_a_downloaded_fastchess_is_checked_and_used(monkeypatch, tmp_path):
    import worker

    monkeypatch.chdir(tmp_path)
    script  = b'#!/bin/sh\necho "fastchess alpha 1.8.2"\n'
    archive = tar_with('fastchess', script)
    monkeypatch.setattr(worker.requests, 'get', lambda url, **kw: Reply(content=archive))

    cfg = config()
    assert worker.download_match_runner_binary(cfg, 'fastchess', runner_data(archive)) is True
    assert cfg.fastchess_ver == '1.8.2'
    assert os.access(tmp_path / 'fastchess-ob', os.X_OK)

    # A checksum that fails leaves what was there alone and says so
    (tmp_path / 'fastchess-ob').write_text('kept')
    assert worker.download_match_runner_binary(cfg, 'fastchess', runner_data(archive, sha='0' * 64)) is False
    assert (tmp_path / 'fastchess-ob').read_text() == 'kept'
    assert not (tmp_path / 'fastchess-ob.part').exists()

    # A binary that won't run is no binary
    broken = tar_with('fastchess', b'not a program')
    monkeypatch.setattr(worker.requests, 'get', lambda url, **kw: Reply(content=broken))
    assert worker.download_match_runner_binary(cfg, 'fastchess', runner_data(broken)) is False


def test_without_a_binary_or_a_compiler_there_is_nothing_to_do(monkeypatch, tmp_path):
    import worker, utils

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(worker.requests, 'post', lambda *a, **kw: Reply({ 'fastchess_min_version' : '1.0.0' }))
    monkeypatch.setattr(worker, 'locate_utility', lambda *a, **kw: None)

    with pytest.raises(utils.OpenBenchMatchRunnerBuildFailedException):
        worker.server_configure_match_runner(config(cxx_comp=None), 'fastchess', None)
