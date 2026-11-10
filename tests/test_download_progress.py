import hashlib
import logging
from types import SimpleNamespace

from aiohttp import web

from rauc_hawkbit.ddi.client import DDIClient
from rauc_hawkbit.rauc_dbus_ddi_client import RaucDBUSDDIClient

PAYLOAD = b'x' * (1024 * 1024)


async def artifact(request):
    return web.Response(body=PAYLOAD, content_type='application/octet-stream')


def create_app():
    app = web.Application()
    app.router.add_route(
        'GET',
        '/DEFAULT/controller/v1/test-target/softwaremodules/1/artifacts/bundle.raucb',
        artifact)
    return app


async def test_get_binary_reports_progress(aiohttp_client, tmp_path):
    client = await aiohttp_client(create_app())
    ddi = DDIClient(client.session, '{}:{}'.format(client.host, client.port),
                    False, None, 'DEFAULT', 'test-target')
    calls = []

    async def progress(downloaded, total):
        calls.append((downloaded, total))

    dl_location = tmp_path / 'bundle.raucb'
    checksum = await ddi.softwaremodules[1].artifacts['bundle.raucb'](
        str(dl_location), progress_callback=progress)

    assert checksum == hashlib.md5(PAYLOAD).hexdigest()
    assert dl_location.read_bytes() == PAYLOAD
    assert calls, 'progress callback was never called'
    assert all(total == len(PAYLOAD) for _, total in calls)
    assert [d for d, _ in calls] == sorted(d for d, _ in calls)
    assert calls[-1][0] == len(PAYLOAD)


async def test_get_binary_without_progress_callback(aiohttp_client, tmp_path):
    client = await aiohttp_client(create_app())
    ddi = DDIClient(client.session, '{}:{}'.format(client.host, client.port),
                    False, None, 'DEFAULT', 'test-target')

    checksum = await ddi.softwaremodules[1].artifacts['bundle.raucb'](
        str(tmp_path / 'bundle.raucb'))

    assert checksum == hashlib.md5(PAYLOAD).hexdigest()


def make_fake_client():
    sent = []

    async def send_progress_feedback(action_id, message, **progress):
        sent.append((action_id, message, progress))

    fake = SimpleNamespace(logger=logging.getLogger('test'),
                           send_progress_feedback=send_progress_feedback)
    return fake, sent


async def test_progress_reporter_throttles_to_steps():
    fake, sent = make_fake_client()
    report = RaucDBUSDDIClient.download_progress_reporter(fake, '42', 10)

    total = 1000
    for downloaded in range(1, total + 1):
        await report(downloaded, total)

    assert [p['cnt'] for _, _, p in sent] == list(range(0, 101, 10))
    assert all(p['of'] == 100 for _, _, p in sent)
    assert all(action_id == '42' for action_id, _, _ in sent)
    assert sent[-1][1] == 'Downloading bundle: 100% (1000 of 1000 bytes)'


async def test_progress_reporter_large_chunks_skip_steps():
    fake, sent = make_fake_client()
    report = RaucDBUSDDIClient.download_progress_reporter(fake, '42', 10)

    for downloaded in (5, 35, 38, 100):
        await report(downloaded, 100)

    assert [p['cnt'] for _, _, p in sent] == [5, 35, 100]


async def test_progress_reporter_ignores_unknown_size():
    fake, sent = make_fake_client()
    report = RaucDBUSDDIClient.download_progress_reporter(fake, '42', 10)

    await report(500, None)

    assert sent == []


async def test_send_progress_feedback_swallows_errors(caplog):
    async def failing_feedback(*args, **kwargs):
        raise ConnectionError('network down')

    fake = SimpleNamespace(
        logger=logging.getLogger('test'),
        ddi=SimpleNamespace(deploymentBase={
            '42': SimpleNamespace(feedback=failing_feedback)}))

    with caplog.at_level(logging.WARNING):
        await RaucDBUSDDIClient.send_progress_feedback(fake, '42', 'hello')

    assert 'Could not send feedback "hello"' in caplog.text
