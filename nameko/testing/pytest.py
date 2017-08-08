from __future__ import absolute_import

import pytest


# all imports are inline to make sure they happen after eventlet.monkey_patch
# which is called in pytest_load_initial_conftests
# (calling monkey_patch at import time breaks the pytest capturemanager - see
#  https://github.com/eventlet/eventlet/pull/239)


def pytest_addoption(parser):
    parser.addoption(
        '--blocking-detection',
        action='store_true',
        dest='blocking_detection',
        default=False,
        help='turn on eventlet hub blocking detection')

    parser.addoption(
        "--log-level", action="store",
        default='DEBUG',
        help=("The logging-level for the test run."))

    parser.addoption(
        "--amqp-uri", "--rabbit-amqp-uri",
        action="store",
        dest='RABBIT_AMQP_URI',
        default='pyamqp://guest:guest@localhost:5672/',
        help=(
            "URI for the RabbitMQ broker. Any specified virtual host will be "
            "ignored because tests run in their own isolated vhost."
        ))

    parser.addoption(
        "--rabbit-api-uri", "--rabbit-ctl-uri",
        action="store",
        dest='RABBIT_API_URI',
        default='http://guest:guest@localhost:15672',
        help=("URI for RabbitMQ management interface.")
    )

    parser.addoption(
        '--amqp-ssl-ca-certs',
        action='store',
        dest='AMQP_SSL_CA_CERTS',
        help='CA certificates chain file for SSL connection')

    parser.addoption(
        '--amqp-ssl-certfile',
        action='store',
        dest='AMQP_SSL_CERTFILE',
        help='Certificate file for SSL connection')

    parser.addoption(
        '--amqp-ssl-keyfile',
        action='store',
        dest='AMQP_SSL_KEYFILE',
        help='Private key file for SSL connection')


def pytest_load_initial_conftests():
    # make sure we monkey_patch before local conftests
    import eventlet
    eventlet.monkey_patch()


def pytest_configure(config):
    import logging
    import sys

    if config.option.blocking_detection:  # pragma: no cover
        from eventlet import debug
        debug.hub_blocking_detection(True)

    log_level = config.getoption('log_level')
    if log_level is not None:
        log_level = getattr(logging, log_level)
        logging.basicConfig(level=log_level, stream=sys.stderr)


@pytest.fixture(autouse=True)
def always_warn_for_deprecation():
    import warnings
    warnings.simplefilter('always', DeprecationWarning)


@pytest.fixture
def empty_config():
    return {}


@pytest.fixture
def mock_container(request, empty_config):
    from mock import create_autospec
    from nameko.constants import SERIALIZER_CONFIG_KEY, DEFAULT_SERIALIZER
    from nameko.containers import ServiceContainer

    container = create_autospec(ServiceContainer)
    container.config = empty_config
    container.config[SERIALIZER_CONFIG_KEY] = DEFAULT_SERIALIZER
    container.serializer = container.config[SERIALIZER_CONFIG_KEY]
    container.accept = [DEFAULT_SERIALIZER]
    return container


@pytest.fixture(scope='session')
def rabbit_manager(request):
    from nameko.testing import rabbit

    config = request.config
    return rabbit.Client(config.getoption('RABBIT_API_URI'))


@pytest.yield_fixture(scope='session')
def vhost_pipeline(request, rabbit_manager):
    from six.moves.urllib.parse import urlparse  # pylint: disable=E0401
    import random
    import string
    from nameko.testing.utils import ResourcePipeline

    rabbit_amqp_uri = request.config.getoption('RABBIT_AMQP_URI')
    uri_parts = urlparse(rabbit_amqp_uri)
    username = uri_parts.username

    def create():
        vhost = "nameko_test_{}".format(
            "".join(random.choice(string.ascii_lowercase) for _ in range(10))
        )
        rabbit_manager.create_vhost(vhost)
        rabbit_manager.set_vhost_permissions(
            vhost, username, '.*', '.*', '.*'
        )
        return vhost

    def destroy(vhost):
        rabbit_manager.delete_vhost(vhost)

    pipeline = ResourcePipeline(create, destroy)

    with pipeline.run() as vhosts:
        yield vhosts


@pytest.yield_fixture()
def rabbit_config(request, vhost_pipeline, rabbit_manager):
    from six.moves.urllib.parse import urlparse  # pylint: disable=E0401

    rabbit_amqp_uri = request.config.getoption('RABBIT_AMQP_URI')
    uri_parts = urlparse(rabbit_amqp_uri)
    username = uri_parts.username

    with vhost_pipeline.get() as vhost:

        amqp_uri = "{uri.scheme}://{uri.netloc}/{vhost}".format(
            uri=uri_parts, vhost=vhost
        )

        conf = {
            'AMQP_URI': amqp_uri,
            'username': username,
            'vhost': vhost
        }

        yield conf


@pytest.fixture()
def rabbit_ssl_config(request):
    from ssl import CERT_REQUIRED # pylint: disable=E0401

    ca_certs = request.config.getoption('AMQP_SSL_CA_CERTS')
    certfile = request.config.getoption('AMQP_SSL_CERTFILE')
    keyfile = request.config.getoption('AMQP_SSL_KEYFILE')

    conf = {
        'AMQP_SSL': {
            'ca_certs': ca_certs,
            'certfile': certfile,
            'keyfile': keyfile,
            'cert_reqs': CERT_REQUIRED,
        },
    }

    return conf


@pytest.yield_fixture(autouse=True)
def fast_teardown(request):
    """
    This fixture fixes the order of the `container_factory`, `runner_factory`
    and `rabbit_config` fixtures to get the fastest possible teardown of tests
    that use them.

    Without this fixture, the teardown order depends on the fixture resolution
    defined by the test, for example::

    def test_foo(container_factory, rabbit_config):
        pass  # rabbit_config tears down first

    def test_bar(rabbit_config, container_factory):
        pass  # container_factory tears down first

    This fixture ensures the teardown order is:

        1. `fast_teardown`  (this fixture)
        2. `rabbit_config`
        3. `container_factory` / `runner_factory`

    That is, `rabbit_config` teardown, which removes the vhost created for
    the test, happens *before* the consumers are stopped.

    Deleting the vhost causes the broker to sends a "basic-cancel" message
    to any connected consumers, which will include the consumers in all
    containers created by the `container_factory` and `runner_factory`
    fixtures.

    This speeds up test teardown because the "basic-cancel" breaks
    the consumers' `drain_events` loop (http://bit.do/kombu-drain-events)
    which would otherwise wait for up to a second for the socket read to time
    out before gracefully shutting down.

    For even faster teardown, we monkeypatch the consumers to ensure they
    don't try to reconnect between the "basic-cancel" and being explicitly
    stopped when their container is killed.

    In older versions of RabbitMQ, the monkeypatch also protects against a
    race-condition that can lead to hanging tests.

    Modern RabbitMQ raises a `NotAllowed` exception if you try to connect to
    a vhost that doesn't exist, but older versions (including 3.4.3, used by
    Travis) just raise a `socket.error`. This is classed as a recoverable
    error, and consumers attempt to reconnect. Kombu's reconnection code blocks
    until a connection is established, so consumers that attempt to reconnect
    before being killed get stuck there.
    """
    from kombu.mixins import ConsumerMixin

    reorder_fixtures = ('container_factory', 'runner_factory', 'rabbit_config')
    for fixture in reorder_fixtures:
        if fixture in request.funcargnames:
            request.getfuncargvalue(fixture)

    consumers = []

    # monkeypatch the ConsumerMixin constructor to stash a reference to
    # each instance
    orig_init = ConsumerMixin.__init__

    def __init__(self, *args, **kwargs):
        orig_init(self, *args, **kwargs)
        consumers.append(self)

    ConsumerMixin.__init__ = __init__

    yield

    ConsumerMixin.__init__ = orig_init

    # set the `should_stop` attribute on all consumers *before* the rabbit
    # vhost is killed, so that they don't try to reconnect before they're
    # explicitly killed when their container stops.
    for consumer in consumers:
        consumer.should_stop = True


@pytest.yield_fixture
def container_factory():
    from nameko.containers import get_container_cls
    import warnings

    all_containers = []

    def make_container(service_cls, config, worker_ctx_cls=None):

        container_cls = get_container_cls(config)

        if worker_ctx_cls is not None:
            warnings.warn(
                "The constructor of `container_factory` has changed. "
                "The `worker_ctx_cls` kwarg is now deprecated. See CHANGES, "
                "Version 2.4.0 for more details.", DeprecationWarning
            )

        container = container_cls(service_cls, config, worker_ctx_cls)
        all_containers.append(container)
        return container

    yield make_container
    for c in all_containers:
        try:
            c.kill()
        except:  # pragma: no cover
            pass


@pytest.yield_fixture
def runner_factory():
    from nameko.runners import ServiceRunner

    all_runners = []

    def make_runner(config, *service_classes):
        runner = ServiceRunner(config)
        for service_cls in service_classes:
            runner.add_service(service_cls)
        all_runners.append(runner)
        return runner

    yield make_runner

    for r in all_runners:
        try:
            r.kill()
        except:  # pragma: no cover
            pass


@pytest.yield_fixture
def predictable_call_ids(request):
    import itertools
    from mock import patch

    with patch('nameko.containers.new_call_id', autospec=True) as get_id:
        get_id.side_effect = (str(i) for i in itertools.count())
        yield get_id


@pytest.fixture()
def web_config(empty_config):
    from nameko.constants import WEB_SERVER_CONFIG_KEY
    from nameko.testing.utils import find_free_port

    port = find_free_port()

    cfg = empty_config
    cfg[WEB_SERVER_CONFIG_KEY] = str(port)
    return cfg


@pytest.fixture()
def web_config_port(web_config):
    from nameko.constants import WEB_SERVER_CONFIG_KEY
    from nameko.web.server import parse_address
    return parse_address(web_config[WEB_SERVER_CONFIG_KEY]).port


@pytest.yield_fixture()
def web_session(web_config_port):
    from requests import Session
    from werkzeug.urls import url_join

    class WebSession(Session):
        def request(self, method, url, *args, **kwargs):
            url = url_join('http://127.0.0.1:%d/' % web_config_port, url)
            return Session.request(self, method, url, *args, **kwargs)

    sess = WebSession()
    with sess:
        yield sess


@pytest.yield_fixture()
def websocket(web_config_port):
    import eventlet
    from nameko.testing.websocket import make_virtual_socket

    active_sockets = []

    def socket_creator():
        ws_app, wait_for_sock = make_virtual_socket(
            '127.0.0.1', web_config_port)
        gr = eventlet.spawn(ws_app.run_forever)
        active_sockets.append((gr, ws_app))
        socket = wait_for_sock()
        socket.app = ws_app
        return socket

    try:
        yield socket_creator
    finally:
        for gr, ws_app in active_sockets:
            try:
                ws_app.close()
            except Exception:  # pragma: no cover
                pass
            gr.kill()
