# -------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License.txt in the project root for
# license information.
# --------------------------------------------------------------------------

from azure.iot.device.common.mqtt_transport import MQTTTransport, OperationManager
from azure.iot.device.common.models.x509 import X509
from azure.iot.device.common import transport_exceptions as errors
from azure.iot.device.common import ProxyOptions
import paho.mqtt.client as mqtt
import ssl
import copy
import pytest
import logging
import socket
import socks
import threading
import gc
import weakref

logging.basicConfig(level=logging.DEBUG)

fake_hostname = "fake.hostname"
fake_device_id = "MyDevice"
fake_password = "fake_password"
fake_username = fake_hostname + "/" + fake_device_id
new_fake_password = "new fake password"
fake_topic = "fake_topic"
fake_payload = "some payload"
fake_cipher = "DHE-RSA-AES128-SHA"
fake_qos = 1
fake_mid = 52
fake_rc = 0
fake_success_rc = 0
fake_failed_rc = mqtt.MQTT_ERR_PROTOCOL
failed_connack_rc = mqtt.CONNACK_REFUSED_IDENTIFIER_REJECTED
fake_keepalive = 1234


# mapping of Paho connack rc codes to Error object classes
connack_return_codes = [
    {
        "name": "CONNACK_REFUSED_PROTOCOL_VERSION",
        "rc": mqtt.CONNACK_REFUSED_PROTOCOL_VERSION,
        "error": errors.ProtocolClientError,
    },
    {
        "name": "CONNACK_REFUSED_IDENTIFIER_REJECTED",
        "rc": mqtt.CONNACK_REFUSED_IDENTIFIER_REJECTED,
        "error": errors.ProtocolClientError,
    },
    {
        "name": "CONNACK_REFUSED_SERVER_UNAVAILABLE",
        "rc": mqtt.CONNACK_REFUSED_SERVER_UNAVAILABLE,
        "error": errors.ConnectionFailedError,
    },
    {
        "name": "CONNACK_REFUSED_BAD_USERNAME_PASSWORD",
        "rc": mqtt.CONNACK_REFUSED_BAD_USERNAME_PASSWORD,
        "error": errors.UnauthorizedError,
    },
    {
        "name": "CONNACK_REFUSED_NOT_AUTHORIZED",
        "rc": mqtt.CONNACK_REFUSED_NOT_AUTHORIZED,
        "error": errors.UnauthorizedError,
    },
]


# mapping of Paho rc codes to Error object classes
operation_return_codes = [
    {"name": "MQTT_ERR_NOMEM", "rc": mqtt.MQTT_ERR_NOMEM, "error": errors.ConnectionDroppedError},
    {
        "name": "MQTT_ERR_PROTOCOL",
        "rc": mqtt.MQTT_ERR_PROTOCOL,
        "error": errors.ProtocolClientError,
    },
    {"name": "MQTT_ERR_INVAL", "rc": mqtt.MQTT_ERR_INVAL, "error": errors.ProtocolClientError},
    {"name": "MQTT_ERR_NO_CONN", "rc": mqtt.MQTT_ERR_NO_CONN, "error": errors.NoConnectionError},
    {
        "name": "MQTT_ERR_CONN_REFUSED",
        "rc": mqtt.MQTT_ERR_CONN_REFUSED,
        "error": errors.ConnectionFailedError,
    },
    {
        "name": "MQTT_ERR_NOT_FOUND",
        "rc": mqtt.MQTT_ERR_NOT_FOUND,
        "error": errors.ConnectionFailedError,
    },
    {
        "name": "MQTT_ERR_CONN_LOST",
        "rc": mqtt.MQTT_ERR_CONN_LOST,
        "error": errors.ConnectionDroppedError,
    },
    {"name": "MQTT_ERR_TLS", "rc": mqtt.MQTT_ERR_TLS, "error": errors.UnauthorizedError},
    {
        "name": "MQTT_ERR_PAYLOAD_SIZE",
        "rc": mqtt.MQTT_ERR_PAYLOAD_SIZE,
        "error": errors.ProtocolClientError,
    },
    {
        "name": "MQTT_ERR_NOT_SUPPORTED",
        "rc": mqtt.MQTT_ERR_NOT_SUPPORTED,
        "error": errors.ProtocolClientError,
    },
    {"name": "MQTT_ERR_AUTH", "rc": mqtt.MQTT_ERR_AUTH, "error": errors.UnauthorizedError},
    {
        "name": "MQTT_ERR_ACL_DENIED",
        "rc": mqtt.MQTT_ERR_ACL_DENIED,
        "error": errors.UnauthorizedError,
    },
    {"name": "MQTT_ERR_UNKNOWN", "rc": mqtt.MQTT_ERR_UNKNOWN, "error": errors.ProtocolClientError},
    {"name": "MQTT_ERR_ERRNO", "rc": mqtt.MQTT_ERR_ERRNO, "error": errors.ProtocolClientError},
    {
        "name": "MQTT_ERR_QUEUE_SIZE",
        "rc": mqtt.MQTT_ERR_QUEUE_SIZE,
        "error": errors.ProtocolClientError,
    },
    {
        "name": "MQTT_ERR_KEEPALIVE",
        "rc": mqtt.MQTT_ERR_KEEPALIVE,
        "error": errors.ConnectionDroppedError,
    },
]


@pytest.fixture
def mock_mqtt_client(mocker, fake_paho_thread):
    mock = mocker.patch.object(mqtt, "Client")
    mock_mqtt_client = mock.return_value
    mock_mqtt_client.subscribe = mocker.MagicMock(return_value=(fake_rc, fake_mid))
    mock_mqtt_client.unsubscribe = mocker.MagicMock(return_value=(fake_rc, fake_mid))
    mock_mqtt_client.publish = mocker.MagicMock(return_value=(fake_rc, fake_mid))
    mock_mqtt_client.connect.return_value = 0
    mock_mqtt_client.reconnect.return_value = 0
    mock_mqtt_client.disconnect.return_value = 0
    mock_mqtt_client._thread = fake_paho_thread
    return mock_mqtt_client


@pytest.fixture
def transport(mock_mqtt_client):
    # Implicitly imports the mocked Paho MQTT Client from mock_mqtt_client
    return MQTTTransport(client_id=fake_device_id, hostname=fake_hostname, username=fake_username)


@pytest.fixture
def fake_paho_thread(mocker):
    thread = mocker.MagicMock(spec=threading.Thread)
    thread.name = "_fake_paho_thread_"
    return thread


@pytest.fixture
def mock_paho_thread_current(mocker, fake_paho_thread):
    return mocker.patch.object(threading, "current_thread", return_value=fake_paho_thread)


@pytest.fixture
def fake_non_paho_thread(mocker):
    thread = mocker.MagicMock(spec=threading.Thread)
    thread.name = "_fake_non_paho_thread_"
    return thread


@pytest.fixture
def mock_non_paho_thread_current(mocker, fake_non_paho_thread):
    return mocker.patch.object(threading, "current_thread", return_value=fake_non_paho_thread)


@pytest.mark.describe("MQTTTransport - Instantiation")
class TestInstantiation(object):
    @pytest.fixture(
        params=["HTTP - No Auth", "HTTP - Auth", "SOCKS4", "SOCKS5 - No Auth", "SOCKS5 - Auth"]
    )
    def proxy_options(self, request):
        if "HTTP" in request.param:
            proxy_type = "HTTP"
        elif "SOCKS4" in request.param:
            proxy_type = "SOCKS4"
        else:
            proxy_type = "SOCKS5"

        if "No Auth" in request.param:
            proxy = ProxyOptions(proxy_type=proxy_type, proxy_addr="fake.address", proxy_port=1080)
        else:
            proxy = ProxyOptions(
                proxy_type=proxy_type,
                proxy_addr="fake.address",
                proxy_port=1080,
                proxy_username="fake_username",
                proxy_password="fake_password",
            )
        return proxy

    @pytest.mark.it("Creates an instance of the Paho MQTT Client")
    def test_instantiates_mqtt_client(self, mocker):
        mock_mqtt_client_constructor = mocker.patch.object(mqtt, "Client")

        MQTTTransport(client_id=fake_device_id, hostname=fake_hostname, username=fake_username)

        assert mock_mqtt_client_constructor.call_count == 1
        assert mock_mqtt_client_constructor.call_args == mocker.call(
            client_id=fake_device_id, clean_session=False, protocol=mqtt.MQTTv311
        )

    @pytest.mark.it(
        "Creates an instance of the Paho MQTT Client using Websockets when websockets parameter is True"
    )
    def test_configures_mqtt_websockets(self, mocker):
        mock_mqtt_client_constructor = mocker.patch.object(mqtt, "Client")
        mock_mqtt_client = mock_mqtt_client_constructor.return_value

        MQTTTransport(
            client_id=fake_device_id,
            hostname=fake_hostname,
            username=fake_username,
            websockets=True,
        )

        assert mock_mqtt_client_constructor.call_count == 1
        assert mock_mqtt_client_constructor.call_args == mocker.call(
            client_id=fake_device_id,
            clean_session=False,
            protocol=mqtt.MQTTv311,
            transport="websockets",
        )

        # Verify websockets options have been set
        assert mock_mqtt_client.ws_set_options.call_count == 1
        assert mock_mqtt_client.ws_set_options.call_args == mocker.call(path="/$iothub/websocket")

    @pytest.mark.it(
        "Sets the proxy information on the client when the `proxy_options` parameter is provided"
    )
    def test_proxy_config(self, mocker, proxy_options):
        mock_mqtt_client_constructor = mocker.patch.object(mqtt, "Client")
        mock_mqtt_client = mock_mqtt_client_constructor.return_value

        MQTTTransport(
            client_id=fake_device_id,
            hostname=fake_hostname,
            username=fake_username,
            proxy_options=proxy_options,
        )

        # Verify proxy has been set
        assert mock_mqtt_client.proxy_set.call_count == 1
        assert mock_mqtt_client.proxy_set.call_args == mocker.call(
            proxy_type=proxy_options.proxy_type_socks,
            proxy_addr=proxy_options.proxy_address,
            proxy_port=proxy_options.proxy_port,
            proxy_username=proxy_options.proxy_username,
            proxy_password=proxy_options.proxy_password,
        )

    @pytest.mark.it(
        "Configures TLS/SSL context to use client-side connection, require certificates and check hostname"
    )
    def test_configures_tls_context(self, mocker):
        mock_mqtt_client = mocker.patch.object(mqtt, "Client").return_value
        mock_ssl_context_constructor = mocker.patch.object(ssl, "SSLContext")
        mock_ssl_context = mock_ssl_context_constructor.return_value

        MQTTTransport(client_id=fake_device_id, hostname=fake_hostname, username=fake_username)

        # Verify correctness of TLS/SSL Context
        assert mock_ssl_context_constructor.call_count == 1
        assert mock_ssl_context_constructor.call_args == mocker.call(
            protocol=ssl.PROTOCOL_TLS_CLIENT
        )
        assert mock_ssl_context.check_hostname is True
        assert mock_ssl_context.verify_mode == ssl.CERT_REQUIRED

        # Verify context has been set
        assert mock_mqtt_client.tls_set_context.call_count == 1
        assert mock_mqtt_client.tls_set_context.call_args == mocker.call(context=mock_ssl_context)

    @pytest.mark.it(
        "Configures TLS/SSL context using default certificates if protocol wrapper not instantiated with a server verification certificate"
    )
    def test_configures_tls_context_with_default_certs(self, mocker, mock_mqtt_client):
        mock_ssl_context_constructor = mocker.patch.object(ssl, "SSLContext")
        mock_ssl_context = mock_ssl_context_constructor.return_value

        MQTTTransport(client_id=fake_device_id, hostname=fake_hostname, username=fake_username)

        assert mock_ssl_context.load_default_certs.call_count == 1
        assert mock_ssl_context.load_default_certs.call_args == mocker.call()

    @pytest.mark.it(
        "Configures TLS/SSL context with provided server verification certificate if protocol wrapper instantiated with a server verification certificate"
    )
    def test_configures_tls_context_with_server_verification_certs(self, mocker, mock_mqtt_client):
        mock_ssl_context_constructor = mocker.patch.object(ssl, "SSLContext")
        mock_ssl_context = mock_ssl_context_constructor.return_value
        server_verification_cert = "dummy_certificate"

        MQTTTransport(
            client_id=fake_device_id,
            hostname=fake_hostname,
            username=fake_username,
            server_verification_cert=server_verification_cert,
        )

        assert mock_ssl_context.load_verify_locations.call_count == 1
        assert mock_ssl_context.load_verify_locations.call_args == mocker.call(
            cadata=server_verification_cert
        )

    @pytest.mark.it(
        "Configures TLS/SSL context with provided cipher if present during instantiation"
    )
    def test_configures_tls_context_with_cipher(self, mocker, mock_mqtt_client):
        mock_ssl_context_constructor = mocker.patch.object(ssl, "SSLContext")
        mock_ssl_context = mock_ssl_context_constructor.return_value

        MQTTTransport(
            client_id=fake_device_id,
            hostname=fake_hostname,
            username=fake_username,
            cipher=fake_cipher,
        )

        assert mock_ssl_context.set_ciphers.call_count == 1
        assert mock_ssl_context.set_ciphers.call_args == mocker.call(fake_cipher)

    @pytest.mark.it("Configures TLS/SSL context with client-provided-certificate-chain like x509")
    def test_configures_tls_context_with_client_provided_certificate_chain(
        self, mocker, mock_mqtt_client
    ):
        mock_ssl_context_constructor = mocker.patch.object(ssl, "SSLContext")
        mock_ssl_context = mock_ssl_context_constructor.return_value
        fake_client_cert = X509("fake_cert_file", "fake_key_file", "fake pass phrase")

        MQTTTransport(
            client_id=fake_device_id,
            hostname=fake_hostname,
            username=fake_username,
            x509_cert=fake_client_cert,
        )

        assert mock_ssl_context.load_default_certs.call_count == 1
        assert mock_ssl_context.load_cert_chain.call_count == 1
        assert mock_ssl_context.load_cert_chain.call_args == mocker.call(
            fake_client_cert.certificate_file,
            fake_client_cert.key_file,
            fake_client_cert.pass_phrase,
        )

    @pytest.mark.it("Sets Paho MQTT Client callbacks")
    def test_sets_paho_callbacks(self, mocker):
        mock_mqtt_client = mocker.patch.object(mqtt, "Client").return_value

        MQTTTransport(client_id=fake_device_id, hostname=fake_hostname, username=fake_username)

        assert callable(mock_mqtt_client.on_connect)
        assert callable(mock_mqtt_client.on_disconnect)
        assert callable(mock_mqtt_client.on_subscribe)
        assert callable(mock_mqtt_client.on_unsubscribe)
        assert callable(mock_mqtt_client.on_publish)
        assert callable(mock_mqtt_client.on_message)

    @pytest.mark.it("Initializes event handlers to 'None'")
    def test_handler_callbacks_set_to_none(self, mocker):
        mocker.patch.object(mqtt, "Client")

        transport = MQTTTransport(
            client_id=fake_device_id, hostname=fake_hostname, username=fake_username
        )

        assert transport.on_mqtt_connected_handler is None
        assert transport.on_mqtt_disconnected_handler is None
        assert transport.on_mqtt_message_received_handler is None

    @pytest.mark.it("Initializes internal operation tracking structures")
    def test_operation_infrastructure_set_up(self, mocker):
        transport = MQTTTransport(
            client_id=fake_device_id, hostname=fake_hostname, username=fake_username
        )
        assert transport._op_manager._pending_operation_callbacks == {}
        assert transport._op_manager._unknown_operation_completions == {}

    @pytest.mark.it("Sets paho auto-reconnect interval to 2 hours")
    def test_sets_reconnect_interval(self, mocker, transport, mock_mqtt_client):
        MQTTTransport(client_id=fake_device_id, hostname=fake_hostname, username=fake_username)

        # called once by the mqtt_client constructor and once by mqtt_transport.py
        assert mock_mqtt_client.reconnect_delay_set.call_count == 2
        assert mock_mqtt_client.reconnect_delay_set.call_args == mocker.call(120 * 60)


@pytest.mark.describe("MQTTTransport - .shutdown()")
class TestShutdown(object):
    @pytest.mark.it("Force Disconnects Paho")
    def test_disconnects(self, mocker, mock_mqtt_client, transport):
        transport.shutdown()

        assert mock_mqtt_client.disconnect.call_count == 1
        assert mock_mqtt_client.disconnect.call_args == mocker.call()
        assert mock_mqtt_client.loop_stop.call_count == 1
        assert mock_mqtt_client.loop_stop.call_args == mocker.call()

    @pytest.mark.it("Does NOT trigger the on_disconnect handler upon disconnect")
    def test_does_not_trigger_handler(self, mocker, mock_mqtt_client, transport):
        mock_disconnect_handler = mocker.MagicMock()
        mock_mqtt_client.on_disconnect = mock_disconnect_handler
        transport.shutdown()
        assert mock_mqtt_client.on_disconnect is None
        assert mock_disconnect_handler.call_count == 0


class ArbitraryConnectException(Exception):
    pass


@pytest.mark.describe("MQTTTransport - .connect()")
class TestConnect(object):
    @pytest.mark.it("Uses the stored username and provided password for Paho credentials")
    def test_use_provided_password(self, mocker, mock_mqtt_client, transport):
        transport.connect(fake_password)

        assert mock_mqtt_client.username_pw_set.call_count == 1
        assert mock_mqtt_client.username_pw_set.call_args == mocker.call(
            username=transport._username, password=fake_password
        )

    @pytest.mark.it(
        "Uses the stored username without a password for Paho credentials, if password is not provided"
    )
    def test_use_no_password(self, mocker, mock_mqtt_client, transport):
        transport.connect()

        assert mock_mqtt_client.username_pw_set.call_count == 1
        assert mock_mqtt_client.username_pw_set.call_args == mocker.call(
            username=transport._username, password=None
        )

    @pytest.mark.it("Initiates MQTT connect via Paho")
    @pytest.mark.parametrize(
        "password",
        [
            pytest.param(fake_password, id="Password provided"),
            pytest.param(None, id="No password provided"),
        ],
    )
    @pytest.mark.parametrize(
        "websockets,port",
        [
            pytest.param(False, 8883, id="Not using websockets"),
            pytest.param(True, 443, id="Using websockets"),
        ],
    )
    def test_calls_paho_connect(
        self, mocker, mock_mqtt_client, transport, password, websockets, port
    ):

        # We don't want to use a special fixture for websockets, so instead we are overriding the attribute below.
        # However, we want to assert that this value is not undefined. For instance, the self._websockets convention private attribute
        # could be changed to self._websockets1, and all our tests would still pass without the below assert statement.
        assert transport._websockets is False

        transport._websockets = websockets
        fake_keepalive = 900
        transport._keep_alive = fake_keepalive

        transport.connect(password)

        assert mock_mqtt_client.connect.call_count == 1
        assert mock_mqtt_client.connect.call_args == mocker.call(
            host=fake_hostname, port=port, keepalive=fake_keepalive
        )

    @pytest.mark.it("Starts MQTT Network Loop")
    @pytest.mark.parametrize(
        "password",
        [
            pytest.param(fake_password, id="Password provided"),
            pytest.param(None, id="No password provided"),
        ],
    )
    def test_calls_loop_start(self, mocker, mock_mqtt_client, transport, password):
        transport.connect(password)

        assert mock_mqtt_client.loop_start.call_count == 1
        assert mock_mqtt_client.loop_start.call_args == mocker.call()

    @pytest.mark.it("Raises a ProtocolClientError if Paho connect raises an unexpected Exception")
    def test_client_raises_unexpected_error(
        self, mocker, mock_mqtt_client, transport, arbitrary_exception
    ):
        mock_mqtt_client.connect.side_effect = arbitrary_exception
        with pytest.raises(errors.ProtocolClientError) as e_info:
            transport.connect(fake_password)
        assert e_info.value.__cause__ is arbitrary_exception

    @pytest.mark.it(
        "Raises a ConnectionFailedError if Paho connect raises a socket.error Exception"
    )
    def test_client_raises_socket_error(
        self, mocker, mock_mqtt_client, transport, arbitrary_exception
    ):
        socket_error = socket.error()
        mock_mqtt_client.connect.side_effect = socket_error
        with pytest.raises(errors.ConnectionFailedError) as e_info:
            transport.connect(fake_password)
        assert e_info.value.__cause__ is socket_error

    @pytest.mark.it(
        "Raises a TlsExchangeAuthError if Paho connect raises a socket.error of type SSLCertVerificationError Exception"
    )
    def test_client_raises_socket_tls_auth_error(
        self, mocker, mock_mqtt_client, transport, arbitrary_exception
    ):
        socket_error = ssl.SSLError("socket error", "CERTIFICATE_VERIFY_FAILED")
        mock_mqtt_client.connect.side_effect = socket_error
        with pytest.raises(errors.TlsExchangeAuthError) as e_info:
            transport.connect(fake_password)
        assert e_info.value.__cause__ is socket_error
        print(e_info.value.__cause__.strerror)

    @pytest.mark.it(
        "Raises a ProtocolProxyError if Paho connect raises a socket error or a ProxyError exception"
    )
    def test_client_raises_socket_error_or_proxy_error_as_proxy_error(
        self, mocker, mock_mqtt_client, transport, arbitrary_exception
    ):
        socks_error = socks.SOCKS5Error(
            "it is a sock 5 error", socket_err="a general SOCKS5Error error"
        )
        mock_mqtt_client.connect.side_effect = socks_error
        with pytest.raises(errors.ProtocolProxyError) as e_info:
            transport.connect(fake_password)
        assert e_info.value.__cause__ is socks_error
        print(e_info.value.__cause__.strerror)

    @pytest.mark.it(
        "Raises a UnauthorizedError if Paho connect raises a socket error or a ProxyError exception"
    )
    def test_client_raises_socket_error_or_proxy_error_as_unauthorized_error(
        self, mocker, mock_mqtt_client, transport, arbitrary_exception
    ):
        socks_error = socks.SOCKS5AuthError(
            "it is a sock 5 auth error", socket_err="an auth SOCKS5Error error"
        )
        mock_mqtt_client.connect.side_effect = socks_error
        with pytest.raises(errors.UnauthorizedError) as e_info:
            transport.connect(fake_password)
        assert e_info.value.__cause__ is socks_error
        print(e_info.value.__cause__.strerror)

    @pytest.mark.it("Allows any BaseExceptions raised in Paho connect to propagate")
    def test_client_raises_base_exception(
        self, mock_mqtt_client, transport, arbitrary_base_exception
    ):
        mock_mqtt_client.connect.side_effect = arbitrary_base_exception
        with pytest.raises(arbitrary_base_exception.__class__) as e_info:
            transport.connect(fake_password)
        assert e_info.value is arbitrary_base_exception

    # NOTE: this test tests for all possible return codes, even ones that shouldn't be
    # possible on a connect operation.
    @pytest.mark.it("Raises a custom Exception if Paho connect returns a failing rc code")
    @pytest.mark.parametrize(
        "error_params",
        operation_return_codes,
        ids=["{}->{}".format(x["name"], x["error"].__name__) for x in operation_return_codes],
    )
    def test_client_returns_failing_rc_code(
        self, mocker, mock_mqtt_client, transport, error_params
    ):
        mock_mqtt_client.connect.return_value = error_params["rc"]
        with pytest.raises(error_params["error"]):
            transport.connect(fake_password)

    @pytest.fixture(
        params=[
            ArbitraryConnectException(),
            socket.error(),
            ssl.SSLError("socket error", "CERTIFICATE_VERIFY_FAILED"),
            socks.SOCKS5Error("it is a sock 5 error", socket_err="a general SOCKS5Error error"),
            socks.SOCKS5AuthError(
                "it is a sock 5 auth error", socket_err="an auth SOCKS5Error error"
            ),
        ],
        ids=[
            "ArbitraryConnectException",
            "socket.error",
            "ssl.SSLError",
            "socks.SOCKS5Error",
            "socks.SOCKS5AuthError",
        ],
    )
    def connect_exception(self, request):
        return request.param

    @pytest.mark.it("Calls _mqtt_client.disconnect if Paho raises an exception")
    def test_calls_disconnect_on_exception(
        self, mocker, mock_mqtt_client, transport, connect_exception
    ):
        mock_mqtt_client.connect.side_effect = connect_exception
        with pytest.raises(Exception):
            transport.connect(fake_password)
        assert mock_mqtt_client.disconnect.call_count == 1

    @pytest.mark.it("Calls _mqtt_client.loop_stop if Paho raises an exception")
    def test_calls_loop_stop_on_exception(
        self, mocker, mock_mqtt_client, transport, connect_exception
    ):
        mock_mqtt_client.connect.side_effect = connect_exception
        with pytest.raises(Exception):
            transport.connect(fake_password)
        assert mock_mqtt_client.loop_stop.call_count == 1

    @pytest.mark.it(
        "Sets Paho's _thread to None if Paho raises an exception while running in the Paho thread"
    )
    def test_sets_thread_to_none_on_exception_in_paho_thread(
        self, mocker, mock_mqtt_client, transport, mock_paho_thread_current, connect_exception
    ):
        mock_mqtt_client.connect.side_effect = connect_exception
        with pytest.raises(Exception):
            transport.connect(fake_password)
        assert mock_mqtt_client._thread is None

    @pytest.mark.it(
        "Does not sets Paho's _thread to None if Paho raises an exception running outside the Paho thread"
    )
    def test_does_not_set_thread_to_none_on_exception_not_in_paho_thread(
        self, mocker, mock_mqtt_client, transport, mock_non_paho_thread_current, connect_exception
    ):
        mock_mqtt_client.connect.side_effect = connect_exception
        with pytest.raises(Exception):
            transport.connect(fake_password)
        assert mock_mqtt_client._thread is not None


@pytest.mark.describe("MQTTTransport - OCCURRENCE: Connect Completed")
class TestEventConnectComplete(object):
    @pytest.mark.it(
        "Triggers on_mqtt_connected_handler event handler upon successful connect completion"
    )
    def test_calls_event_handler_callback(self, mocker, mock_mqtt_client, transport):
        callback = mocker.MagicMock()
        transport.on_mqtt_connected_handler = callback

        # Manually trigger Paho on_connect event_handler
        mock_mqtt_client.on_connect(client=mock_mqtt_client, userdata=None, flags=None, rc=fake_rc)

        # Verify transport.on_mqtt_connected_handler was called
        assert callback.call_count == 1
        assert callback.call_args == mocker.call()

    @pytest.mark.it(
        "Skips on_mqtt_connected_handler event handler if set to 'None' upon successful connect completion"
    )
    def test_skips_none_event_handler_callback(self, mocker, mock_mqtt_client, transport):
        assert transport.on_mqtt_connected_handler is None

        transport.connect(fake_password)

        mock_mqtt_client.on_connect(client=mock_mqtt_client, userdata=None, flags=None, rc=fake_rc)

        # No further asserts required - this is a test to show that it skips a callback.
        # Not raising an exception == test passed

    @pytest.mark.it("Recovers from Exception in on_mqtt_connected_handler event handler")
    def test_event_handler_callback_raises_exception(
        self, mocker, mock_mqtt_client, transport, arbitrary_exception
    ):
        event_cb = mocker.MagicMock(side_effect=arbitrary_exception)
        transport.on_mqtt_connected_handler = event_cb

        transport.connect(fake_password)
        mock_mqtt_client.on_connect(client=mock_mqtt_client, userdata=None, flags=None, rc=fake_rc)

        # Callback was called, but exception did not propagate
        assert event_cb.call_count == 1

    @pytest.mark.it(
        "Allows any BaseExceptions raised in on_mqtt_connected_handler event handler to propagate"
    )
    def test_event_handler_callback_raises_base_exception(
        self, mocker, mock_mqtt_client, transport, arbitrary_base_exception
    ):
        event_cb = mocker.MagicMock(side_effect=arbitrary_base_exception)
        transport.on_mqtt_connected_handler = event_cb

        transport.connect(fake_password)
        with pytest.raises(arbitrary_base_exception.__class__) as e_info:
            mock_mqtt_client.on_connect(
                client=mock_mqtt_client, userdata=None, flags=None, rc=fake_rc
            )
        assert e_info.value is arbitrary_base_exception


@pytest.mark.describe("MQTTTransport - OCCURRENCE: Connection Failure")
class TestEventConnectionFailure(object):
    @pytest.mark.parametrize(
        "error_params",
        connack_return_codes,
        ids=["{}->{}".format(x["name"], x["error"].__name__) for x in connack_return_codes],
    )
    @pytest.mark.it(
        "Triggers on_mqtt_connection_failure_handler event handler with custom Exception upon failed connect completion"
    )
    def test_calls_event_handler_callback_with_failed_rc(
        self, mocker, mock_mqtt_client, transport, error_params
    ):
        callback = mocker.MagicMock()
        transport.on_mqtt_connection_failure_handler = callback

        # Initiate connect
        transport.connect(fake_password)

        # Manually trigger Paho on_connect event_handler
        mock_mqtt_client.on_connect(
            client=mock_mqtt_client, userdata=None, flags=None, rc=error_params["rc"]
        )

        # Verify transport.on_mqtt_connection_failure_handler was called
        assert callback.call_count == 1
        assert isinstance(callback.call_args[0][0], error_params["error"])

    @pytest.mark.it(
        "Skips on_mqtt_connection_failure_handler event handler if set to 'None' upon failed connect completion"
    )
    def test_skips_none_event_handler_callback(self, mocker, mock_mqtt_client, transport):
        assert transport.on_mqtt_connection_failure_handler is None

        transport.connect(fake_password)

        mock_mqtt_client.on_connect(
            client=mock_mqtt_client, userdata=None, flags=None, rc=failed_connack_rc
        )

        # No further asserts required - this is a test to show that it skips a callback.
        # Not raising an exception == test passed

    @pytest.mark.it("Recovers from Exception in on_mqtt_connection_failure_handler event handler")
    def test_event_handler_callback_raises_exception(
        self, mocker, mock_mqtt_client, transport, arbitrary_exception
    ):
        event_cb = mocker.MagicMock(side_effect=arbitrary_exception)
        transport.on_mqtt_connection_failure_handler = event_cb

        transport.connect(fake_password)
        mock_mqtt_client.on_connect(
            client=mock_mqtt_client, userdata=None, flags=None, rc=failed_connack_rc
        )

        # Callback was called, but exception did not propagate
        assert event_cb.call_count == 1

    @pytest.mark.it(
        "Allows any BaseExceptions raised in on_mqtt_connection_failure_handler event handler to propagate"
    )
    def test_event_handler_callback_raises_base_exception(
        self, mocker, mock_mqtt_client, transport, arbitrary_base_exception
    ):
        event_cb = mocker.MagicMock(side_effect=arbitrary_base_exception)
        transport.on_mqtt_connection_failure_handler = event_cb

        transport.connect(fake_password)
        with pytest.raises(arbitrary_base_exception.__class__) as e_info:
            mock_mqtt_client.on_connect(
                client=mock_mqtt_client, userdata=None, flags=None, rc=failed_connack_rc
            )
        assert e_info.value is arbitrary_base_exception


@pytest.mark.describe("MQTTTransport - .disconnect()")
class TestDisconnect(object):
    @pytest.mark.it("Initiates MQTT disconnect via Paho")
    def test_calls_paho_disconnect(self, mocker, mock_mqtt_client, transport):
        transport.disconnect()

        assert mock_mqtt_client.disconnect.call_count == 1
        assert mock_mqtt_client.disconnect.call_args == mocker.call()

    @pytest.mark.it(
        "Raises a ProtocolClientError if Paho disconnect raises an unexpected Exception"
    )
    def test_client_raises_unexpected_error(
        self, mocker, mock_mqtt_client, transport, arbitrary_exception
    ):
        mock_mqtt_client.disconnect.side_effect = arbitrary_exception
        with pytest.raises(errors.ProtocolClientError) as e_info:
            transport.disconnect()
        assert e_info.value.__cause__ is arbitrary_exception

    @pytest.mark.it("Allows any BaseExceptions raised in Paho disconnect to propagate")
    def test_client_raises_base_exception(
        self, mock_mqtt_client, transport, arbitrary_base_exception
    ):
        mock_mqtt_client.disconnect.side_effect = arbitrary_base_exception
        with pytest.raises(arbitrary_base_exception.__class__) as e_info:
            transport.disconnect()
        assert e_info.value is arbitrary_base_exception

    @pytest.mark.it("Raises a custom Exception if Paho disconnect returns a failing rc code")
    @pytest.mark.parametrize(
        "error_params",
        operation_return_codes,
        ids=["{}->{}".format(x["name"], x["error"].__name__) for x in operation_return_codes],
    )
    def test_client_returns_failing_rc_code(
        self, mocker, mock_mqtt_client, transport, error_params
    ):
        mock_mqtt_client.disconnect.return_value = error_params["rc"]
        with pytest.raises(error_params["error"]):
            transport.disconnect()

    @pytest.mark.it("Cancels all pending operations if the clear_inflight parameter is True")
    def test_pending_op_cancellation(self, mocker, mock_mqtt_client, transport):
        # Set up a pending publish
        pub_callback = mocker.MagicMock(name="pub cb")
        pub_mid = "1"
        message_info = mqtt.MQTTMessageInfo(pub_mid)
        message_info.rc = fake_rc
        mock_mqtt_client.publish.return_value = message_info
        transport.publish(topic=fake_topic, payload=fake_payload, callback=pub_callback)

        # Set up a pending subscribe
        sub_callback = mocker.MagicMock(name="sub_cb")
        sub_mid = "2"
        mock_mqtt_client.subscribe.return_value = (fake_rc, sub_mid)
        transport.subscribe(topic=fake_topic, qos=fake_qos, callback=sub_callback)

        # Operations are pending
        assert pub_callback.call_count == 0
        assert sub_callback.call_count == 0

        # Disconnect and clear pending ops
        transport.disconnect(clear_inflight=True)

        # Pending operations were cancelled
        assert pub_callback.call_count == 1
        assert pub_callback.call_args == mocker.call(cancelled=True)
        assert sub_callback.call_count == 1
        assert sub_callback.call_args == mocker.call(cancelled=True)

    @pytest.mark.it(
        "Does not cancel any pending operations if the clear_inflight parameter is False"
    )
    def test_no_pending_op_cancellation(self, mocker, mock_mqtt_client, transport):
        # Set up a pending publish
        pub_callback = mocker.MagicMock(name="pub cb")
        pub_mid = "1"
        message_info = mqtt.MQTTMessageInfo(pub_mid)
        message_info.rc = fake_rc
        mock_mqtt_client.publish.return_value = message_info
        transport.publish(topic=fake_topic, payload=fake_payload, callback=pub_callback)

        # Set up a pending subscribe
        sub_callback = mocker.MagicMock(name="sub_cb")
        sub_mid = "2"
        mock_mqtt_client.subscribe.return_value = (fake_rc, sub_mid)
        transport.subscribe(topic=fake_topic, qos=fake_qos, callback=sub_callback)

        # Operations are pending
        assert pub_callback.call_count == 0
        assert sub_callback.call_count == 0

        # Disconnect
        transport.disconnect(clear_inflight=False)

        # No pending operations were cancelled
        assert pub_callback.call_count == 0
        assert sub_callback.call_count == 0

    @pytest.mark.it(
        "Does not cancel any pending operations if the clear_inflight parameter is not provided"
    )
    def test_default_no_pending_op_cancellation(self, mocker, mock_mqtt_client, transport):
        # Set up a pending publish
        pub_callback = mocker.MagicMock(name="pub cb")
        pub_mid = "1"
        message_info = mqtt.MQTTMessageInfo(pub_mid)
        message_info.rc = fake_rc
        mock_mqtt_client.publish.return_value = message_info
        transport.publish(topic=fake_topic, payload=fake_payload, callback=pub_callback)

        # Set up a pending subscribe
        sub_callback = mocker.MagicMock(name="sub_cb")
        sub_mid = "2"
        mock_mqtt_client.subscribe.return_value = (fake_rc, sub_mid)
        transport.subscribe(topic=fake_topic, qos=fake_qos, callback=sub_callback)

        # Operations are pending
        assert pub_callback.call_count == 0
        assert sub_callback.call_count == 0

        # Disconnect
        transport.disconnect()

        # No pending operations were cancelled
        assert pub_callback.call_count == 0
        assert sub_callback.call_count == 0

    @pytest.mark.it("Stops MQTT Network Loop when disconnect does not raise an exception")
    def test_calls_loop_stop_on_success(self, mocker, mock_mqtt_client, transport):
        transport.disconnect()

        assert mock_mqtt_client.loop_stop.call_count == 1
        assert mock_mqtt_client.loop_stop.call_args == mocker.call()

    @pytest.mark.it("Stops MQTT Network Loop when disconnect raises an exception")
    def test_calls_loop_stop_on_exception(
        self, mocker, mock_mqtt_client, transport, arbitrary_exception
    ):
        mock_mqtt_client.disconnect.side_effect = arbitrary_exception

        with pytest.raises(Exception):
            transport.disconnect()

        assert mock_mqtt_client.loop_stop.call_count == 1
        assert mock_mqtt_client.loop_stop.call_args == mocker.call()

    @pytest.mark.it(
        "Sets Paho's _thread to None if disconnect does not raise an exception while running in the Paho thread"
    )
    def test_sets_thread_to_none_on_success_in_paho_thread(
        self, mocker, mock_mqtt_client, transport, mock_paho_thread_current
    ):
        transport.disconnect()
        assert mock_mqtt_client._thread is None

    @pytest.mark.it(
        "Sets Paho's _thread to None if disconnect raises an exception while running in the Paho thread"
    )
    def test_sets_thread_to_none_on_exception_in_paho_thread(
        self, mocker, mock_mqtt_client, transport, arbitrary_exception, mock_paho_thread_current
    ):
        mock_mqtt_client.disconnect.side_effect = arbitrary_exception

        with pytest.raises(Exception):
            transport.disconnect()
        assert mock_mqtt_client._thread is None

    @pytest.mark.it(
        "Does not set Paho's _thread to None if disconnect does not raise an exception while running outside the Paho thread"
    )
    def test_does_not_set_thread_to_none_on_success_in_non_paho_thread(
        self, mocker, mock_mqtt_client, transport, mock_non_paho_thread_current
    ):
        transport.disconnect()
        assert mock_mqtt_client._thread is not None

    @pytest.mark.it(
        "Does not set  Paho's _thread to None if disconnect raises an exception while running outside the Paho thread"
    )
    def test_does_not_set_thread_to_none_on_exception_in_non_paho_thread(
        self, mocker, mock_mqtt_client, transport, arbitrary_exception, mock_non_paho_thread_current
    ):
        mock_mqtt_client.disconnect.side_effect = arbitrary_exception

        with pytest.raises(Exception):
            transport.disconnect()
        assert mock_mqtt_client._thread is not None


@pytest.mark.describe("MQTTTransport - OCCURRENCE: Disconnect Completed")
class TestEventDisconnectCompleted(object):
    @pytest.fixture
    def collected_transport_weakref(self, mock_mqtt_client):
        # return a weak reference to an MQTTTransport that has already been collected
        transport = MQTTTransport(
            client_id=fake_device_id, hostname=fake_hostname, username=fake_username
        )
        transport_weakref = weakref.ref(transport)
        transport = None
        gc.collect(2)  # 2 == collect as much as possible
        assert transport_weakref() is None
        return transport_weakref

    @pytest.fixture(
        params=[fake_success_rc, fake_failed_rc], ids=["success rc code", "failed rc code"]
    )
    def rc_success_or_failure(self, request):
        return request.param

    @pytest.mark.it(
        "Triggers on_mqtt_disconnected_handler event handler upon disconnect completion"
    )
    def test_calls_event_handler_callback_externally_driven(
        self, mocker, mock_mqtt_client, transport
    ):
        callback = mocker.MagicMock()
        transport.on_mqtt_disconnected_handler = callback

        # Initiate disconnect
        transport.disconnect()

        # Manually trigger Paho on_connect event_handler
        mock_mqtt_client.on_disconnect(client=mock_mqtt_client, userdata=None, rc=fake_rc)

        # Verify transport.on_mqtt_connected_handler was called
        assert callback.call_count == 1
        assert callback.call_args == mocker.call(None)

    @pytest.mark.parametrize(
        "error_params",
        operation_return_codes,
        ids=["{}->{}".format(x["name"], x["error"].__name__) for x in operation_return_codes],
    )
    @pytest.mark.it(
        "Triggers on_mqtt_disconnected_handler event handler with custom Exception when an error RC is returned upon disconnect completion."
    )
    def test_calls_event_handler_callback_with_failure_user_driven(
        self, mocker, mock_mqtt_client, transport, error_params
    ):
        callback = mocker.MagicMock()
        transport.on_mqtt_disconnected_handler = callback

        # Initiate disconnect
        transport.disconnect()

        # Manually trigger Paho on_disconnect event_handler
        mock_mqtt_client.on_disconnect(
            client=mock_mqtt_client, userdata=None, rc=error_params["rc"]
        )

        # Verify transport.on_mqtt_disconnected_handler was called
        assert callback.call_count == 1
        assert isinstance(callback.call_args[0][0], error_params["error"])

    @pytest.mark.it(
        "Skips on_mqtt_disconnected_handler event handler if set to 'None' upon disconnect completion"
    )
    def test_skips_none_event_handler_callback(self, mocker, mock_mqtt_client, transport):
        assert transport.on_mqtt_disconnected_handler is None

        transport.disconnect()

        mock_mqtt_client.on_disconnect(client=mock_mqtt_client, userdata=None, rc=fake_rc)

        # No further asserts required - this is a test to show that it skips a callback.
        # Not raising an exception == test passed

    @pytest.mark.it("Recovers from Exception in on_mqtt_disconnected_handler event handler")
    def test_event_handler_callback_raises_exception(
        self, mocker, mock_mqtt_client, transport, arbitrary_exception
    ):
        event_cb = mocker.MagicMock(side_effect=arbitrary_exception)
        transport.on_mqtt_disconnected_handler = event_cb

        transport.disconnect()
        mock_mqtt_client.on_disconnect(client=mock_mqtt_client, userdata=None, rc=fake_rc)

        # Callback was called, but exception did not propagate
        assert event_cb.call_count == 1

    @pytest.mark.it(
        "Allows any BaseExceptions raised in on_mqtt_disconnected_handler event handler to propagate"
    )
    def test_event_handler_callback_raises_base_exception(
        self, mocker, mock_mqtt_client, transport, arbitrary_base_exception
    ):
        event_cb = mocker.MagicMock(side_effect=arbitrary_base_exception)
        transport.on_mqtt_disconnected_handler = event_cb

        transport.disconnect()
        with pytest.raises(arbitrary_base_exception.__class__) as e_info:
            mock_mqtt_client.on_disconnect(client=mock_mqtt_client, userdata=None, rc=fake_rc)
        assert e_info.value is arbitrary_base_exception

    @pytest.mark.it("Calls Paho's disconnect() method if cause is not None")
    def test_calls_disconnect_with_cause(self, mock_mqtt_client, transport):
        mock_mqtt_client.on_disconnect(client=mock_mqtt_client, userdata=None, rc=fake_failed_rc)
        assert mock_mqtt_client.disconnect.call_count == 1

    @pytest.mark.it("Does not call Paho's disconnect() method if cause is None")
    def test_doesnt_call_disconnect_without_cause(self, mock_mqtt_client, transport):
        mock_mqtt_client.on_disconnect(client=mock_mqtt_client, userdata=None, rc=fake_success_rc)
        assert mock_mqtt_client.disconnect.call_count == 0

    @pytest.mark.it("Calls Paho's loop_stop() if cause is not None")
    def test_calls_loop_stop(self, mock_mqtt_client, transport):
        mock_mqtt_client.on_disconnect(client=mock_mqtt_client, userdata=None, rc=fake_failed_rc)
        assert mock_mqtt_client.loop_stop.call_count == 1

    @pytest.mark.it("Does not calls Paho's loop_stop() if cause is None")
    def test_does_not_call_loop_stop(self, mock_mqtt_client, transport):
        mock_mqtt_client.on_disconnect(client=mock_mqtt_client, userdata=None, rc=fake_success_rc)
        assert mock_mqtt_client.loop_stop.call_count == 0

    @pytest.mark.it(
        "Sets Paho's _thread to None if cause is not None while running in the Paho thread"
    )
    def test_sets_thread_to_none_on_failure_in_paho_thread(
        self, mock_mqtt_client, transport, mock_paho_thread_current
    ):
        mock_mqtt_client.on_disconnect(client=mock_mqtt_client, userdata=None, rc=fake_failed_rc)
        assert mock_mqtt_client._thread is None

    @pytest.mark.it(
        "Does not set Paho's _thread to None if cause is not None while running outside the paho thread"
    )
    def test_sets_thread_to_none_on_failure_in_non_paho_thread(
        self, mock_mqtt_client, transport, mock_non_paho_thread_current
    ):
        mock_mqtt_client.on_disconnect(client=mock_mqtt_client, userdata=None, rc=fake_failed_rc)
        assert mock_mqtt_client._thread is not None

    @pytest.mark.it(
        "Does not sets Paho's _thread to None if cause is None while running in the Paho thread"
    )
    def test_does_not_set_thread_to_none_on_success_in_paho_thread(
        self, mock_mqtt_client, transport, mock_paho_thread_current
    ):
        mock_mqtt_client.on_disconnect(client=mock_mqtt_client, userdata=None, rc=fake_success_rc)
        assert mock_mqtt_client._thread is not None

    @pytest.mark.it(
        "Does not sets Paho's _thread to None if cause is None while running outside the Paho thread"
    )
    def test_does_not_set_thread_to_none_on_success_in_non_paho_thread(
        self, mock_mqtt_client, transport, mock_non_paho_thread_current
    ):
        mock_mqtt_client.on_disconnect(client=mock_mqtt_client, userdata=None, rc=fake_success_rc)
        assert mock_mqtt_client._thread is not None

    @pytest.mark.it("Allows any Exception raised by Paho's disconnect() to propagate")
    def test_disconnect_raises_exception(
        self, mock_mqtt_client, transport, mocker, arbitrary_exception
    ):
        mock_mqtt_client.disconnect = mocker.MagicMock(side_effect=arbitrary_exception)
        with pytest.raises(type(arbitrary_exception)) as e_info:
            mock_mqtt_client.on_disconnect(
                client=mock_mqtt_client, userdata=None, rc=fake_failed_rc
            )
        assert e_info.value is arbitrary_exception

    @pytest.mark.it("Allows any BaseException raised by Paho's disconnect() to propagate")
    def test_disconnect_raises_base_exception(
        self, mock_mqtt_client, transport, mocker, arbitrary_base_exception
    ):
        mock_mqtt_client.disconnect = mocker.MagicMock(side_effect=arbitrary_base_exception)
        with pytest.raises(type(arbitrary_base_exception)) as e_info:
            mock_mqtt_client.on_disconnect(
                client=mock_mqtt_client, userdata=None, rc=fake_failed_rc
            )
        assert e_info.value is arbitrary_base_exception

    @pytest.mark.it("Allows any Exception raised by Paho's loop_stop() to propagate")
    def test_loop_stop_raises_exception(
        self, mock_mqtt_client, transport, mocker, arbitrary_exception
    ):
        mock_mqtt_client.loop_stop = mocker.MagicMock(side_effect=arbitrary_exception)
        with pytest.raises(type(arbitrary_exception)) as e_info:
            mock_mqtt_client.on_disconnect(
                client=mock_mqtt_client, userdata=None, rc=fake_failed_rc
            )
        assert e_info.value is arbitrary_exception

    @pytest.mark.it("Allows any BaseException raised by Paho's loop_stop() to propagate")
    def test_loop_stop_raises_base_exception(
        self, mock_mqtt_client, transport, mocker, arbitrary_base_exception
    ):
        mock_mqtt_client.loop_stop = mocker.MagicMock(side_effect=arbitrary_base_exception)
        with pytest.raises(type(arbitrary_base_exception)) as e_info:
            mock_mqtt_client.on_disconnect(
                client=mock_mqtt_client, userdata=None, rc=fake_failed_rc
            )
        assert e_info.value is arbitrary_base_exception

    @pytest.mark.it(
        "Does not raise any exceptions if the MQTTTransport object was garbage collected before the disconnect completed"
    )
    def test_no_exception_after_gc(
        self, mock_mqtt_client, collected_transport_weakref, rc_success_or_failure
    ):
        assert mock_mqtt_client.on_disconnect
        mock_mqtt_client.on_disconnect(mock_mqtt_client, None, rc_success_or_failure)
        # lack of exception is success

    @pytest.mark.it(
        "Calls Paho's loop_stop() if the MQTTTransport object was garbage collected before the disconnect completed"
    )
    def test_calls_loop_stop_after_gc(
        self, collected_transport_weakref, mock_mqtt_client, rc_success_or_failure, mocker
    ):
        assert mock_mqtt_client.loop_stop.call_count == 0
        mock_mqtt_client.on_disconnect(mock_mqtt_client, None, rc_success_or_failure)
        assert mock_mqtt_client.loop_stop.call_count == 1
        assert mock_mqtt_client.loop_stop.call_args == mocker.call()

    @pytest.mark.it(
        "Allows any Exception raised by Paho's loop_stop() to propagate if the MQTTTransport object was garbage collected before the disconnect completed"
    )
    def test_raises_exception_after_gc(
        self,
        collected_transport_weakref,
        mock_mqtt_client,
        rc_success_or_failure,
        arbitrary_exception,
    ):
        mock_mqtt_client.loop_stop.side_effect = arbitrary_exception
        with pytest.raises(type(arbitrary_exception)):
            mock_mqtt_client.on_disconnect(mock_mqtt_client, None, rc_success_or_failure)

    @pytest.mark.it(
        "Allows any BaseException raised by Paho's loop_stop() to propagate if the MQTTTransport object was garbage collected before the disconnect completed"
    )
    def test_raises_base_exception_after_gc(
        self,
        collected_transport_weakref,
        mock_mqtt_client,
        rc_success_or_failure,
        arbitrary_base_exception,
    ):
        mock_mqtt_client.loop_stop.side_effect = arbitrary_base_exception
        with pytest.raises(type(arbitrary_base_exception)):
            mock_mqtt_client.on_disconnect(mock_mqtt_client, None, rc_success_or_failure)


@pytest.mark.describe("MQTTTransport - .subscribe()")
class TestSubscribe(object):
    @pytest.mark.it("Subscribes with Paho")
    @pytest.mark.parametrize(
        "qos",
        [pytest.param(0, id="QoS 0"), pytest.param(1, id="QoS 1"), pytest.param(2, id="QoS 2")],
    )
    def test_calls_paho_subscribe(self, mocker, mock_mqtt_client, transport, qos):
        transport.subscribe(fake_topic, qos=qos)

        assert mock_mqtt_client.subscribe.call_count == 1
        assert mock_mqtt_client.subscribe.call_args == mocker.call(fake_topic, qos=qos)

    @pytest.mark.it("Raises ValueError on invalid QoS")
    @pytest.mark.parametrize("qos", [pytest.param(-1, id="QoS < 0"), pytest.param(3, id="QoS > 2")])
    def test_raises_value_error_invalid_qos(self, qos):
        # Manually instantiate protocol wrapper, do NOT mock paho client (paho generates this error)
        transport = MQTTTransport(
            client_id=fake_device_id, hostname=fake_hostname, username=fake_username
        )
        with pytest.raises(ValueError):
            transport.subscribe(fake_topic, qos=qos)

    @pytest.mark.it("Raises ValueError on invalid topic string")
    @pytest.mark.parametrize("topic", [pytest.param(None), pytest.param("", id="Empty string")])
    def test_raises_value_error_invalid_topic(self, topic):
        # Manually instantiate protocol wrapper, do NOT mock paho client (paho generates this error)
        transport = MQTTTransport(
            client_id=fake_device_id, hostname=fake_hostname, username=fake_username
        )
        with pytest.raises(ValueError):
            transport.subscribe(topic, qos=fake_qos)

    @pytest.mark.it("Triggers callback upon subscribe completion")
    def test_triggers_callback_upon_paho_on_subscribe_event(
        self, mocker, mock_mqtt_client, transport
    ):
        callback = mocker.MagicMock()
        mock_mqtt_client.subscribe.return_value = (fake_rc, fake_mid)

        # Initiate subscribe
        transport.subscribe(topic=fake_topic, qos=fake_qos, callback=callback)

        # Check callback is not called yet
        assert callback.call_count == 0

        # Manually trigger Paho on_subscribe event handler
        mock_mqtt_client.on_subscribe(
            client=mock_mqtt_client, userdata=None, mid=fake_mid, granted_qos=fake_qos
        )

        # Check callback has now been called
        assert callback.call_count == 1

    @pytest.mark.it(
        "Triggers callback upon subscribe completion when Paho event handler triggered early"
    )
    def test_triggers_callback_when_paho_on_subscribe_event_called_early(
        self, mocker, mock_mqtt_client, transport
    ):
        callback = mocker.MagicMock()

        def trigger_early_on_subscribe(topic, qos):

            # Trigger on_subscribe before returning mid
            mock_mqtt_client.on_subscribe(
                client=mock_mqtt_client, userdata=None, mid=fake_mid, granted_qos=fake_qos
            )

            # Check callback not yet called
            assert callback.call_count == 0

            return (fake_rc, fake_mid)

        mock_mqtt_client.subscribe.side_effect = trigger_early_on_subscribe

        # Initiate subscribe
        transport.subscribe(topic=fake_topic, qos=fake_qos, callback=callback)

        # Check callback has now been called
        assert callback.call_count == 1

    @pytest.mark.it("Skips callback that is set to 'None' upon subscribe completion")
    def test_none_callback_upon_paho_on_subscribe_event(self, mocker, mock_mqtt_client, transport):
        callback = None
        mock_mqtt_client.subscribe.return_value = (fake_rc, fake_mid)

        # Initiate subscribe
        transport.subscribe(topic=fake_topic, qos=fake_qos, callback=callback)

        # Manually trigger Paho on_subscribe event handler
        mock_mqtt_client.on_subscribe(
            client=mock_mqtt_client, userdata=None, mid=fake_mid, granted_qos=fake_qos
        )

        # No assertions necessary - not raising an exception => success

    @pytest.mark.it(
        "Skips callback that is set to 'None' upon subscribe completion when Paho event handler triggered early"
    )
    def test_none_callback_when_paho_on_subscribe_event_called_early(
        self, mocker, mock_mqtt_client, transport
    ):
        callback = None

        def trigger_early_on_subscribe(topic, qos):

            # Trigger on_subscribe before returning mid
            mock_mqtt_client.on_subscribe(
                client=mock_mqtt_client, userdata=None, mid=fake_mid, granted_qos=fake_qos
            )

            return (fake_rc, fake_mid)

        mock_mqtt_client.subscribe.side_effect = trigger_early_on_subscribe

        # Initiate subscribe
        transport.subscribe(topic=fake_topic, qos=fake_qos, callback=callback)

        # No assertions necessary - not raising an exception => success

    @pytest.mark.it(
        "Handles multiple callbacks from multiple subscribe operations that complete out of order"
    )
    def test_multiple_callbacks(self, mocker, mock_mqtt_client, transport):
        callback1 = mocker.MagicMock()
        callback2 = mocker.MagicMock()
        callback3 = mocker.MagicMock()

        mid1 = 1
        mid2 = 2
        mid3 = 3

        mock_mqtt_client.subscribe.side_effect = [(fake_rc, mid1), (fake_rc, mid2), (fake_rc, mid3)]

        # Initiate subscribe (1 -> 2 -> 3)
        transport.subscribe(topic=fake_topic, qos=fake_qos, callback=callback1)
        transport.subscribe(topic=fake_topic, qos=fake_qos, callback=callback2)
        transport.subscribe(topic=fake_topic, qos=fake_qos, callback=callback3)

        # Check callbacks have not yet been called
        assert callback1.call_count == 0
        assert callback2.call_count == 0
        assert callback3.call_count == 0

        # Manually trigger Paho on_subscribe event handler (2 -> 3 -> 1)
        mock_mqtt_client.on_subscribe(
            client=mock_mqtt_client, userdata=None, mid=mid2, granted_qos=fake_qos
        )
        assert callback1.call_count == 0
        assert callback2.call_count == 1
        assert callback3.call_count == 0

        mock_mqtt_client.on_subscribe(
            client=mock_mqtt_client, userdata=None, mid=mid3, granted_qos=fake_qos
        )
        assert callback1.call_count == 0
        assert callback2.call_count == 1
        assert callback3.call_count == 1

        mock_mqtt_client.on_subscribe(
            client=mock_mqtt_client, userdata=None, mid=mid1, granted_qos=fake_qos
        )
        assert callback1.call_count == 1
        assert callback2.call_count == 1
        assert callback3.call_count == 1

    @pytest.mark.it("Recovers from Exception in callback")
    def test_callback_raises_exception(
        self, mocker, mock_mqtt_client, transport, arbitrary_exception
    ):
        callback = mocker.MagicMock(side_effect=arbitrary_exception)
        mock_mqtt_client.subscribe.return_value = (fake_rc, fake_mid)

        transport.subscribe(topic=fake_topic, qos=fake_qos, callback=callback)
        mock_mqtt_client.on_subscribe(
            client=mock_mqtt_client, userdata=None, mid=fake_mid, granted_qos=fake_qos
        )

        # Callback was called, but exception did not propagate
        assert callback.call_count == 1

    @pytest.mark.it("Allows any BaseExceptions raised in callback to propagate")
    def test_callback_raises_base_exception(
        self, mocker, mock_mqtt_client, transport, arbitrary_base_exception
    ):
        callback = mocker.MagicMock(side_effect=arbitrary_base_exception)
        mock_mqtt_client.subscribe.return_value = (fake_rc, fake_mid)

        transport.subscribe(topic=fake_topic, qos=fake_qos, callback=callback)
        with pytest.raises(arbitrary_base_exception.__class__) as e_info:
            mock_mqtt_client.on_subscribe(
                client=mock_mqtt_client, userdata=None, mid=fake_mid, granted_qos=fake_qos
            )
        assert e_info.value is arbitrary_base_exception

    @pytest.mark.it("Recovers from Exception in callback when Paho event handler triggered early")
    def test_callback_raises_exception_when_paho_on_subscribe_triggered_early(
        self, mocker, mock_mqtt_client, transport, arbitrary_exception
    ):
        callback = mocker.MagicMock(side_effect=arbitrary_exception)

        def trigger_early_on_subscribe(topic, qos):
            mock_mqtt_client.on_subscribe(
                client=mock_mqtt_client, userdata=None, mid=fake_mid, granted_qos=fake_qos
            )

            # Should not have yet called callback
            assert callback.call_count == 0

            return (fake_rc, fake_mid)

        mock_mqtt_client.subscribe.side_effect = trigger_early_on_subscribe

        # Initiate subscribe
        transport.subscribe(topic=fake_topic, qos=fake_qos, callback=callback)

        # Callback was called, but exception did not propagate
        assert callback.call_count == 1

    @pytest.mark.it(
        "Allows any BaseExceptions raised in callback when Paho event handler triggered early to propagate"
    )
    def test_callback_raises_base_exception_when_paho_on_subscribe_triggered_early(
        self, mocker, mock_mqtt_client, transport, arbitrary_base_exception
    ):
        callback = mocker.MagicMock(side_effect=arbitrary_base_exception)

        def trigger_early_on_subscribe(topic, qos):
            mock_mqtt_client.on_subscribe(
                client=mock_mqtt_client, userdata=None, mid=fake_mid, granted_qos=fake_qos
            )

            # Should not have yet called callback
            assert callback.call_count == 0

            return (fake_rc, fake_mid)

        mock_mqtt_client.subscribe.side_effect = trigger_early_on_subscribe

        # Initiate subscribe
        with pytest.raises(arbitrary_base_exception.__class__) as e_info:
            transport.subscribe(topic=fake_topic, qos=fake_qos, callback=callback)
        assert e_info.value is arbitrary_base_exception

    @pytest.mark.it("Raises a ProtocolClientError if Paho subscribe raises an unexpected Exception")
    def test_client_raises_unexpected_error(
        self, mocker, mock_mqtt_client, transport, arbitrary_exception
    ):
        mock_mqtt_client.subscribe.side_effect = arbitrary_exception
        with pytest.raises(errors.ProtocolClientError) as e_info:
            transport.subscribe(topic=fake_topic, qos=fake_qos, callback=None)
        assert e_info.value.__cause__ is arbitrary_exception

    @pytest.mark.it("Allows any BaseExceptions raised in Paho subscribe to propagate")
    def test_client_raises_base_exception(
        self, mock_mqtt_client, transport, arbitrary_base_exception
    ):
        mock_mqtt_client.subscribe.side_effect = arbitrary_base_exception
        with pytest.raises(arbitrary_base_exception.__class__) as e_info:
            transport.subscribe(topic=fake_topic, qos=fake_qos, callback=None)
        assert e_info.value is arbitrary_base_exception

    # NOTE: this test tests for all possible return codes, even ones that shouldn't be
    # possible on a subscribe operation.
    @pytest.mark.it("Raises a custom Exception if Paho subscribe returns a failing rc code")
    @pytest.mark.parametrize(
        "error_params",
        operation_return_codes,
        ids=["{}->{}".format(x["name"], x["error"].__name__) for x in operation_return_codes],
    )
    def test_client_returns_failing_rc_code(
        self, mocker, mock_mqtt_client, transport, error_params
    ):
        mock_mqtt_client.subscribe.return_value = (error_params["rc"], 0)
        with pytest.raises(error_params["error"]):
            transport.subscribe(topic=fake_topic, qos=fake_qos, callback=None)


@pytest.mark.describe("MQTTTransport - .unsubscribe()")
class TestUnsubscribe(object):
    @pytest.mark.it("Unsubscribes with Paho")
    def test_calls_paho_unsubscribe(self, mocker, mock_mqtt_client, transport):
        transport.unsubscribe(fake_topic)

        assert mock_mqtt_client.unsubscribe.call_count == 1
        assert mock_mqtt_client.unsubscribe.call_args == mocker.call(fake_topic)

    @pytest.mark.it("Raises ValueError on invalid topic string")
    @pytest.mark.parametrize("topic", [pytest.param(None), pytest.param("", id="Empty string")])
    def test_raises_value_error_invalid_topic(self, topic):
        # Manually instantiate protocol wrapper, do NOT mock paho client (paho generates this error)
        transport = MQTTTransport(
            client_id=fake_device_id, hostname=fake_hostname, username=fake_username
        )
        with pytest.raises(ValueError):
            transport.unsubscribe(topic)

    @pytest.mark.it("Triggers callback upon unsubscribe completion")
    def test_triggers_callback_upon_paho_on_unsubscribe_event(
        self, mocker, mock_mqtt_client, transport
    ):
        callback = mocker.MagicMock()
        mock_mqtt_client.unsubscribe.return_value = (fake_rc, fake_mid)

        # Initiate unsubscribe
        transport.unsubscribe(topic=fake_topic, callback=callback)

        # Check callback not called
        assert callback.call_count == 0

        # Manually trigger Paho on_unsubscribe event handler
        mock_mqtt_client.on_unsubscribe(client=mock_mqtt_client, userdata=None, mid=fake_mid)

        # Check callback has now been called
        assert callback.call_count == 1

    @pytest.mark.it(
        "Triggers callback upon unsubscribe completion when Paho event handler triggered early"
    )
    def test_triggers_callback_when_paho_on_unsubscribe_event_called_early(
        self, mocker, mock_mqtt_client, transport
    ):
        callback = mocker.MagicMock()

        def trigger_early_on_unsubscribe(topic):

            # Trigger on_unsubscribe before returning mid
            mock_mqtt_client.on_unsubscribe(client=mock_mqtt_client, userdata=None, mid=fake_mid)

            # Check callback not yet called
            assert callback.call_count == 0

            return (fake_rc, fake_mid)

        mock_mqtt_client.unsubscribe.side_effect = trigger_early_on_unsubscribe

        # Initiate unsubscribe
        transport.unsubscribe(topic=fake_topic, callback=callback)

        # Check callback has now been called
        assert callback.call_count == 1

    @pytest.mark.it("Skips callback that is set to 'None' upon unsubscribe completion")
    def test_none_callback_upon_paho_on_unsubscribe_event(
        self, mocker, mock_mqtt_client, transport
    ):
        callback = None
        mock_mqtt_client.unsubscribe.return_value = (fake_rc, fake_mid)

        # Initiate unsubscribe
        transport.unsubscribe(topic=fake_topic, callback=callback)

        # Manually trigger Paho on_unsubscribe event handler
        mock_mqtt_client.on_unsubscribe(client=mock_mqtt_client, userdata=None, mid=fake_mid)

        # No assertions necessary - not raising an exception => success

    @pytest.mark.it(
        "Skips callback that is set to 'None' upon unsubscribe completion when Paho event handler triggered early"
    )
    def test_none_callback_when_paho_on_unsubscribe_event_called_early(
        self, mocker, mock_mqtt_client, transport
    ):
        callback = None

        def trigger_early_on_unsubscribe(topic):

            # Trigger on_unsubscribe before returning mid
            mock_mqtt_client.on_unsubscribe(client=mock_mqtt_client, userdata=None, mid=fake_mid)

            return (fake_rc, fake_mid)

        mock_mqtt_client.unsubscribe.side_effect = trigger_early_on_unsubscribe

        # Initiate unsubscribe
        transport.unsubscribe(topic=fake_topic, callback=callback)

        # No assertions necessary - not raising an exception => success

    @pytest.mark.it(
        "Handles multiple callbacks from multiple unsubscribe operations that complete out of order"
    )
    def test_multiple_callbacks(self, mocker, mock_mqtt_client, transport):
        callback1 = mocker.MagicMock()
        callback2 = mocker.MagicMock()
        callback3 = mocker.MagicMock()

        mid1 = 1
        mid2 = 2
        mid3 = 3

        mock_mqtt_client.unsubscribe.side_effect = [
            (fake_rc, mid1),
            (fake_rc, mid2),
            (fake_rc, mid3),
        ]

        # Initiate unsubscribe (1 -> 2 -> 3)
        transport.unsubscribe(topic=fake_topic, callback=callback1)
        transport.unsubscribe(topic=fake_topic, callback=callback2)
        transport.unsubscribe(topic=fake_topic, callback=callback3)

        # Check callbacks have not yet been called
        assert callback1.call_count == 0
        assert callback2.call_count == 0
        assert callback3.call_count == 0

        # Manually trigger Paho on_unsubscribe event handler (2 -> 3 -> 1)
        mock_mqtt_client.on_unsubscribe(client=mock_mqtt_client, userdata=None, mid=mid2)
        assert callback1.call_count == 0
        assert callback2.call_count == 1
        assert callback3.call_count == 0

        mock_mqtt_client.on_unsubscribe(client=mock_mqtt_client, userdata=None, mid=mid3)
        assert callback1.call_count == 0
        assert callback2.call_count == 1
        assert callback3.call_count == 1

        mock_mqtt_client.on_unsubscribe(client=mock_mqtt_client, userdata=None, mid=mid1)
        assert callback1.call_count == 1
        assert callback2.call_count == 1
        assert callback3.call_count == 1

    @pytest.mark.it("Recovers from Exception in callback")
    def test_callback_raises_exception(
        self, mocker, mock_mqtt_client, transport, arbitrary_exception
    ):
        callback = mocker.MagicMock(side_effect=arbitrary_exception)
        mock_mqtt_client.unsubscribe.return_value = (fake_rc, fake_mid)

        transport.unsubscribe(topic=fake_topic, callback=callback)
        mock_mqtt_client.on_unsubscribe(client=mock_mqtt_client, userdata=None, mid=fake_mid)

        # Callback was called, but exception did not propagate
        assert callback.call_count == 1

    @pytest.mark.it("Allows any BaseExceptions raised in callback to propagate")
    def test_callback_raises_base_exception(
        self, mocker, mock_mqtt_client, transport, arbitrary_base_exception
    ):
        callback = mocker.MagicMock(side_effect=arbitrary_base_exception)
        mock_mqtt_client.unsubscribe.return_value = (fake_rc, fake_mid)

        transport.unsubscribe(topic=fake_topic, callback=callback)
        with pytest.raises(arbitrary_base_exception.__class__) as e_info:
            mock_mqtt_client.on_unsubscribe(client=mock_mqtt_client, userdata=None, mid=fake_mid)
        assert e_info.value is arbitrary_base_exception

    @pytest.mark.it("Recovers from Exception in callback when Paho event handler triggered early")
    def test_callback_raises_exception_when_paho_on_unsubscribe_triggered_early(
        self, mocker, mock_mqtt_client, transport, arbitrary_exception
    ):
        callback = mocker.MagicMock(side_effect=arbitrary_exception)

        def trigger_early_on_unsubscribe(topic):
            mock_mqtt_client.on_unsubscribe(client=mock_mqtt_client, userdata=None, mid=fake_mid)

            # Should not have yet called callback
            assert callback.call_count == 0

            return (fake_rc, fake_mid)

        mock_mqtt_client.unsubscribe.side_effect = trigger_early_on_unsubscribe

        # Initiate unsubscribe
        transport.unsubscribe(topic=fake_topic, callback=callback)

        # Callback was called, but exception did not propagate
        assert callback.call_count == 1

    @pytest.mark.it(
        "Allows any BaseExceptions raised in callback when Paho event handler triggered early to propagate"
    )
    def test_callback_raises_base_exception_when_paho_on_unsubscribe_triggered_early(
        self, mocker, mock_mqtt_client, transport, arbitrary_base_exception
    ):
        callback = mocker.MagicMock(side_effect=arbitrary_base_exception)

        def trigger_early_on_unsubscribe(topic):
            mock_mqtt_client.on_unsubscribe(client=mock_mqtt_client, userdata=None, mid=fake_mid)

            # Should not have yet called callback
            assert callback.call_count == 0

            return (fake_rc, fake_mid)

        mock_mqtt_client.unsubscribe.side_effect = trigger_early_on_unsubscribe

        # Initiate unsubscribe
        with pytest.raises(arbitrary_base_exception.__class__) as e_info:
            transport.unsubscribe(topic=fake_topic, callback=callback)
        assert e_info.value is arbitrary_base_exception

    @pytest.mark.it(
        "Raises a ProtocolClientError if Paho unsubscribe raises an unexpected Exception"
    )
    def test_client_raises_unexpected_error(
        self, mocker, mock_mqtt_client, transport, arbitrary_exception
    ):
        mock_mqtt_client.unsubscribe.side_effect = arbitrary_exception
        with pytest.raises(errors.ProtocolClientError) as e_info:
            transport.unsubscribe(topic=fake_topic, callback=None)
        assert e_info.value.__cause__ is arbitrary_exception

    @pytest.mark.it("Allows any BaseExceptions raised in Paho unsubscribe to propagate")
    def test_client_raises_base_exception(
        self, mock_mqtt_client, transport, arbitrary_base_exception
    ):
        mock_mqtt_client.unsubscribe.side_effect = arbitrary_base_exception
        with pytest.raises(arbitrary_base_exception.__class__) as e_info:
            transport.unsubscribe(topic=fake_topic, callback=None)
        assert e_info.value is arbitrary_base_exception

    # NOTE: this test tests for all possible return codes, even ones that shouldn't be
    # possible on an unsubscribe operation.
    @pytest.mark.it("Raises a custom Exception if Paho unsubscribe returns a failing rc code")
    @pytest.mark.parametrize(
        "error_params",
        operation_return_codes,
        ids=["{}->{}".format(x["name"], x["error"].__name__) for x in operation_return_codes],
    )
    def test_client_returns_failing_rc_code(
        self, mocker, mock_mqtt_client, transport, error_params
    ):
        mock_mqtt_client.unsubscribe.return_value = (error_params["rc"], 0)
        with pytest.raises(error_params["error"]):
            transport.unsubscribe(topic=fake_topic, callback=None)


@pytest.mark.describe("MQTTTransport - .publish()")
class TestPublish(object):
    @pytest.fixture
    def message_info(self, mocker):
        mi = mqtt.MQTTMessageInfo(fake_mid)
        mi.rc = fake_rc
        return mi

    @pytest.mark.it("Publishes with Paho")
    @pytest.mark.parametrize(
        "qos",
        [pytest.param(0, id="QoS 0"), pytest.param(1, id="QoS 1"), pytest.param(2, id="QoS 2")],
    )
    def test_calls_paho_publish(self, mocker, mock_mqtt_client, transport, qos):
        transport.publish(topic=fake_topic, payload=fake_payload, qos=qos)

        assert mock_mqtt_client.publish.call_count == 1
        assert mock_mqtt_client.publish.call_args == mocker.call(
            topic=fake_topic, payload=fake_payload, qos=qos
        )

    @pytest.mark.it("Raises ValueError on invalid QoS")
    @pytest.mark.parametrize("qos", [pytest.param(-1, id="QoS < 0"), pytest.param(3, id="Qos > 2")])
    def test_raises_value_error_invalid_qos(self, qos):
        # Manually instantiate protocol wrapper, do NOT mock paho client (paho generates this error)
        transport = MQTTTransport(
            client_id=fake_device_id, hostname=fake_hostname, username=fake_username
        )
        with pytest.raises(ValueError):
            transport.publish(topic=fake_topic, payload=fake_payload, qos=qos)

    @pytest.mark.it("Raises ValueError on invalid topic string")
    @pytest.mark.parametrize(
        "topic",
        [
            pytest.param(None),
            pytest.param("", id="Empty string"),
            pytest.param("+", id="Contains wildcard (+)"),
        ],
    )
    def test_raises_value_error_invalid_topic(self, topic):
        # Manually instantiate protocol wrapper, do NOT mock paho client (paho generates this error)
        transport = MQTTTransport(
            client_id=fake_device_id, hostname=fake_hostname, username=fake_username
        )
        with pytest.raises(ValueError):
            transport.publish(topic=topic, payload=fake_payload, qos=fake_qos)

    @pytest.mark.it("Raises ValueError on invalid payload value")
    @pytest.mark.parametrize("payload", [str(b"0" * 268435456)], ids=["Payload > 268435455 bytes"])
    def test_raises_value_error_invalid_payload(self, payload):
        # Manually instantiate protocol wrapper, do NOT mock paho client (paho generates this error)
        transport = MQTTTransport(
            client_id=fake_device_id, hostname=fake_hostname, username=fake_username
        )
        with pytest.raises(ValueError):
            transport.publish(topic=fake_topic, payload=payload, qos=fake_qos)

    @pytest.mark.it("Raises TypeError on invalid payload type")
    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param({"a": "b"}, id="Dictionary"),
            pytest.param([1, 2, 3], id="List"),
            pytest.param(object(), id="Object"),
        ],
    )
    def test_raises_type_error_invalid_payload_type(self, payload):
        # Manually instantiate protocol wrapper, do NOT mock paho client (paho generates this error)
        transport = MQTTTransport(
            client_id=fake_device_id, hostname=fake_hostname, username=fake_username
        )
        with pytest.raises(TypeError):
            transport.publish(topic=fake_topic, payload=payload, qos=fake_qos)

    @pytest.mark.it("Triggers callback upon publish completion")
    def test_triggers_callback_upon_paho_on_publish_event(
        self, mocker, mock_mqtt_client, transport, message_info
    ):
        callback = mocker.MagicMock()
        mock_mqtt_client.publish.return_value = message_info

        # Initiate publish
        transport.publish(topic=fake_topic, payload=fake_payload, callback=callback)

        # Check callback is not called
        assert callback.call_count == 0

        # Manually trigger Paho on_publish event handler
        mock_mqtt_client.on_publish(client=mock_mqtt_client, userdata=None, mid=message_info.mid)

        # Check callback has now been called
        assert callback.call_count == 1

    @pytest.mark.it(
        "Triggers callback upon publish completion when Paho event handler triggered early"
    )
    def test_triggers_callback_when_paho_on_publish_event_called_early(
        self, mocker, mock_mqtt_client, transport, message_info
    ):
        callback = mocker.MagicMock()

        def trigger_early_on_publish(topic, payload, qos):

            # Trigger on_publish before returning message_info
            mock_mqtt_client.on_publish(
                client=mock_mqtt_client, userdata=None, mid=message_info.mid
            )

            # Check callback not yet called
            assert callback.call_count == 0

            return message_info

        mock_mqtt_client.publish.side_effect = trigger_early_on_publish

        # Initiate publish
        transport.publish(topic=fake_topic, payload=fake_payload, callback=callback)

        # Check callback has now been called
        assert callback.call_count == 1

    @pytest.mark.it("Skips callback that is set to 'None' upon publish completion")
    def test_none_callback_upon_paho_on_publish_event(
        self, mocker, mock_mqtt_client, transport, message_info
    ):
        mock_mqtt_client.publish.return_value = message_info
        callback = None

        # Initiate publish
        transport.publish(topic=fake_topic, payload=fake_payload, callback=callback)

        # Manually trigger Paho on_publish event handler
        mock_mqtt_client.on_publish(client=mock_mqtt_client, userdata=None, mid=message_info.mid)

        # No assertions necessary - not raising an exception => success

    @pytest.mark.it(
        "Skips callback that is set to 'None' upon publish completion when Paho event handler triggered early"
    )
    def test_none_callback_when_paho_on_publish_event_called_early(
        self, mocker, mock_mqtt_client, transport, message_info
    ):
        callback = None

        def trigger_early_on_publish(topic, payload, qos):

            # Trigger on_publish before returning message_info
            mock_mqtt_client.on_publish(
                client=mock_mqtt_client, userdata=None, mid=message_info.mid
            )

            return message_info

        mock_mqtt_client.publish.side_effect = trigger_early_on_publish

        # Initiate publish
        transport.publish(topic=fake_topic, payload=fake_payload, callback=callback)

        # No assertions necessary - not raising an exception => success

    @pytest.mark.it(
        "Handles multiple callbacks from multiple publish operations that complete out of order"
    )
    def test_multiple_callbacks(self, mocker, mock_mqtt_client, transport):
        callback1 = mocker.MagicMock()
        callback2 = mocker.MagicMock()
        callback3 = mocker.MagicMock()

        mid1 = 1
        mid2 = 2
        mid3 = 3

        mock_mqtt_client.publish.side_effect = [
            mqtt.MQTTMessageInfo(mid1),
            mqtt.MQTTMessageInfo(mid2),
            mqtt.MQTTMessageInfo(mid3),
        ]

        # Initiate publish (1 -> 2 -> 3)
        transport.publish(topic=fake_topic, payload=fake_payload, callback=callback1)
        transport.publish(topic=fake_topic, payload=fake_payload, callback=callback2)
        transport.publish(topic=fake_topic, payload=fake_payload, callback=callback3)

        # Check callbacks have not yet been called
        assert callback1.call_count == 0
        assert callback2.call_count == 0
        assert callback3.call_count == 0

        # Manually trigger Paho on_publish event handler (2 -> 3 -> 1)
        mock_mqtt_client.on_publish(client=mock_mqtt_client, userdata=None, mid=mid2)
        assert callback1.call_count == 0
        assert callback2.call_count == 1
        assert callback3.call_count == 0

        mock_mqtt_client.on_publish(client=mock_mqtt_client, userdata=None, mid=mid3)
        assert callback1.call_count == 0
        assert callback2.call_count == 1
        assert callback3.call_count == 1

        mock_mqtt_client.on_publish(client=mock_mqtt_client, userdata=None, mid=mid1)
        assert callback1.call_count == 1
        assert callback2.call_count == 1
        assert callback3.call_count == 1

    @pytest.mark.it("Recovers from Exception in callback")
    def test_callback_raises_exception(
        self, mocker, mock_mqtt_client, transport, message_info, arbitrary_exception
    ):
        callback = mocker.MagicMock(side_effect=arbitrary_exception)
        mock_mqtt_client.publish.return_value = message_info

        transport.publish(topic=fake_topic, payload=fake_payload, callback=callback)
        mock_mqtt_client.on_publish(client=mock_mqtt_client, userdata=None, mid=message_info.mid)

        # Callback was called, but exception did not propagate
        assert callback.call_count == 1

    @pytest.mark.it("Allows any BaseExceptions raised in callback to propagate")
    def test_callback_raises_base_exception(
        self, mocker, mock_mqtt_client, transport, message_info, arbitrary_base_exception
    ):
        callback = mocker.MagicMock(side_effect=arbitrary_base_exception)
        mock_mqtt_client.publish.return_value = message_info

        transport.publish(topic=fake_topic, payload=fake_payload, callback=callback)
        with pytest.raises(arbitrary_base_exception.__class__) as e_info:
            mock_mqtt_client.on_publish(
                client=mock_mqtt_client, userdata=None, mid=message_info.mid
            )
        assert e_info.value is arbitrary_base_exception

    @pytest.mark.it("Recovers from Exception in callback when Paho event handler triggered early")
    def test_callback_raises_exception_when_paho_on_publish_triggered_early(
        self, mocker, mock_mqtt_client, transport, message_info, arbitrary_exception
    ):
        callback = mocker.MagicMock(side_effect=arbitrary_exception)

        def trigger_early_on_publish(topic, payload, qos):
            mock_mqtt_client.on_publish(
                client=mock_mqtt_client, userdata=None, mid=message_info.mid
            )

            # Should not have yet called callback
            assert callback.call_count == 0

            return message_info

        mock_mqtt_client.publish.side_effect = trigger_early_on_publish

        # Initiate publish
        transport.publish(topic=fake_topic, payload=fake_payload, callback=callback)

        # Callback was called, but exception did not propagate
        assert callback.call_count == 1

    @pytest.mark.it(
        "Allows any BaseExceptions raised in callback when Paho event handler triggered early to propagate"
    )
    def test_callback_raises_base_exception_when_paho_on_publish_triggered_early(
        self, mocker, mock_mqtt_client, transport, message_info, arbitrary_base_exception
    ):
        callback = mocker.MagicMock(side_effect=arbitrary_base_exception)

        def trigger_early_on_publish(topic, payload, qos):
            mock_mqtt_client.on_publish(
                client=mock_mqtt_client, userdata=None, mid=message_info.mid
            )

            # Should not have yet called callback
            assert callback.call_count == 0

            return message_info

        mock_mqtt_client.publish.side_effect = trigger_early_on_publish

        # Initiate publish
        with pytest.raises(arbitrary_base_exception.__class__) as e_info:
            transport.publish(topic=fake_topic, payload=fake_payload, callback=callback)
        assert e_info.value is arbitrary_base_exception

    @pytest.mark.it("Raises a ProtocolClientError if Paho publish raises an unexpected Exception")
    def test_client_raises_unexpected_error(
        self, mocker, mock_mqtt_client, transport, arbitrary_exception
    ):
        mock_mqtt_client.publish.side_effect = arbitrary_exception
        with pytest.raises(errors.ProtocolClientError) as e_info:
            transport.publish(topic=fake_topic, payload=fake_payload, callback=None)
        assert e_info.value.__cause__ is arbitrary_exception

    @pytest.mark.it("Allows any BaseExceptions raised in Paho publish to propagate")
    def test_client_raises_base_exception(
        self, mock_mqtt_client, transport, arbitrary_base_exception
    ):
        mock_mqtt_client.publish.side_effect = arbitrary_base_exception
        with pytest.raises(arbitrary_base_exception.__class__) as e_info:
            transport.publish(topic=fake_topic, payload=fake_payload, callback=None)
        assert e_info.value is arbitrary_base_exception

    # NOTE: this test tests for all possible return codes, even ones that shouldn't be
    # possible on a publish operation.
    @pytest.mark.it("Raises a custom Exception if Paho publish returns a failing rc code")
    @pytest.mark.parametrize(
        "error_params",
        operation_return_codes,
        ids=["{}->{}".format(x["name"], x["error"].__name__) for x in operation_return_codes],
    )
    def test_client_returns_failing_rc_code(
        self, mocker, mock_mqtt_client, transport, error_params
    ):
        mock_mqtt_client.publish.return_value = (error_params["rc"], 0)
        with pytest.raises(error_params["error"]):
            transport.publish(topic=fake_topic, payload=fake_payload, callback=None)


@pytest.mark.describe("MQTTTransport - OCCURRENCE: Message Received")
class TestMessageReceived(object):
    @pytest.fixture()
    def message(self):
        message = mqtt.MQTTMessage(mid=fake_mid, topic=fake_topic.encode())
        message.payload = fake_payload
        message.qos = fake_qos
        return message

    @pytest.mark.it(
        "Triggers on_mqtt_message_received_handler event handler upon receiving message"
    )
    def test_calls_event_handler_callback(self, mocker, mock_mqtt_client, transport, message):
        callback = mocker.MagicMock()
        transport.on_mqtt_message_received_handler = callback

        # Manually trigger Paho on_message event_handler
        mock_mqtt_client.on_message(client=mock_mqtt_client, userdata=None, mqtt_message=message)

        # Verify transport.on_mqtt_message_received_handler was called
        assert callback.call_count == 1
        assert callback.call_args == mocker.call(message.topic, message.payload)

    @pytest.mark.it(
        "Skips on_mqtt_message_received_handler event handler if set to 'None' upon receiving message"
    )
    def test_skips_none_event_handler_callback(self, mocker, mock_mqtt_client, transport, message):
        assert transport.on_mqtt_message_received_handler is None

        # Manually trigger Paho on_message event_handler
        mock_mqtt_client.on_message(client=mock_mqtt_client, userdata=None, mqtt_message=message)

        # No further asserts required - this is a test to show that it skips a callback.
        # Not raising an exception == test passed

    @pytest.mark.it("Recovers from Exception in on_mqtt_message_received_handler event handler")
    def test_event_handler_callback_raises_exception(
        self, mocker, mock_mqtt_client, transport, message, arbitrary_exception
    ):
        event_cb = mocker.MagicMock(side_effect=arbitrary_exception)
        transport.on_mqtt_message_received_handler = event_cb

        mock_mqtt_client.on_message(client=mock_mqtt_client, userdata=None, mqtt_message=message)

        # Callback was called, but exception did not propagate
        assert event_cb.call_count == 1

    @pytest.mark.it(
        "Allows any BaseExceptions raised in on_mqtt_message_received_handler event handler to propagate"
    )
    def test_event_handler_callback_raises_base_exception(
        self, mocker, mock_mqtt_client, transport, message, arbitrary_base_exception
    ):
        event_cb = mocker.MagicMock(side_effect=arbitrary_base_exception)
        transport.on_mqtt_message_received_handler = event_cb

        with pytest.raises(arbitrary_base_exception.__class__) as e_info:
            mock_mqtt_client.on_message(
                client=mock_mqtt_client, userdata=None, mqtt_message=message
            )
        assert e_info.value is arbitrary_base_exception


@pytest.mark.describe("MQTTTransport - Misc.")
class TestMisc(object):
    @pytest.mark.it(
        "Handles multiple callbacks from multiple different types of operations that complete out of order"
    )
    def test_multiple_callbacks_multiple_ops(self, mocker, mock_mqtt_client, transport):
        callback1 = mocker.MagicMock()
        callback2 = mocker.MagicMock()
        callback3 = mocker.MagicMock()

        mid1 = 1
        mid2 = 2
        mid3 = 3

        topic1 = "topic1"
        topic2 = "topic2"
        topic3 = "topic3"

        mock_mqtt_client.subscribe.return_value = (fake_rc, mid1)
        mock_mqtt_client.publish.return_value = mqtt.MQTTMessageInfo(mid2)
        mock_mqtt_client.unsubscribe.return_value = (fake_rc, mid3)

        # Initiate operations (1 -> 2 -> 3)
        transport.subscribe(topic=topic1, qos=fake_qos, callback=callback1)
        transport.publish(topic=topic2, payload="payload", qos=fake_qos, callback=callback2)
        transport.unsubscribe(topic=topic3, callback=callback3)

        # Check callbacks have not yet been called
        assert callback1.call_count == 0
        assert callback2.call_count == 0
        assert callback3.call_count == 0

        # Manually trigger Paho on_unsubscribe event handler (2 -> 3 -> 1)
        mock_mqtt_client.on_publish(client=mock_mqtt_client, userdata=None, mid=mid2)
        assert callback1.call_count == 0
        assert callback2.call_count == 1
        assert callback3.call_count == 0

        mock_mqtt_client.on_unsubscribe(client=mock_mqtt_client, userdata=None, mid=mid3)
        assert callback1.call_count == 0
        assert callback2.call_count == 1
        assert callback3.call_count == 1

        mock_mqtt_client.on_subscribe(
            client=mock_mqtt_client, userdata=None, mid=mid1, granted_qos=fake_qos
        )
        assert callback1.call_count == 1
        assert callback2.call_count == 1
        assert callback3.call_count == 1


@pytest.mark.describe("OperationManager")
class TestOperationManager(object):
    @pytest.mark.it("Instantiates with no operation tracking information")
    def test_instantiates_empty(self):
        manager = OperationManager()
        assert len(manager._pending_operation_callbacks) == 0
        assert len(manager._unknown_operation_completions) == 0


@pytest.mark.describe("OperationManager - .establish_operation()")
class TestOperationManagerEstablishOperation(object):
    @pytest.fixture(params=[True, False])
    def optional_callback(self, mocker, request):
        if request.param:
            return mocker.MagicMock()
        else:
            return None

    @pytest.mark.it("Begins tracking a pending operation for a new MID")
    @pytest.mark.parametrize(
        "optional_callback",
        [pytest.param(True, id="With callback"), pytest.param(False, id="No callback")],
        indirect=True,
    )
    def test_no_early_completion(self, optional_callback):
        manager = OperationManager()
        mid = 1
        manager.establish_operation(mid, optional_callback)

        assert len(manager._pending_operation_callbacks) == 1
        assert manager._pending_operation_callbacks[mid] is optional_callback

    @pytest.mark.it(
        "Resolves operation tracking when MID corresponds to a previous unknown completion"
    )
    def test_early_completion(self):
        manager = OperationManager()
        mid = 1

        # Cause early completion of an unknown operation
        manager.complete_operation(mid)
        assert len(manager._unknown_operation_completions) == 1
        assert manager._unknown_operation_completions[mid]

        # Establish operation that was already completed
        manager.establish_operation(mid)

        assert len(manager._unknown_operation_completions) == 0

    @pytest.mark.it(
        "Triggers the callback if provided when MID corresponds to a previous unknown completion"
    )
    def test_early_completion_with_callback(self, mocker):
        manager = OperationManager()
        mid = 1
        cb_mock = mocker.MagicMock()

        # Cause early completion of an unknown operation
        manager.complete_operation(mid)

        # Establish operation that was already completed
        manager.establish_operation(mid, cb_mock)

        assert cb_mock.call_count == 1

    @pytest.mark.it("Recovers from Exception thrown in callback")
    def test_callback_raises_exception(self, mocker, arbitrary_exception):
        manager = OperationManager()
        mid = 1
        cb_mock = mocker.MagicMock(side_effect=arbitrary_exception)

        # Cause early completion of an unknown operation
        manager.complete_operation(mid)

        # Establish operation that was already completed
        manager.establish_operation(mid, cb_mock)

        # Callback was called, but exception did not propagate
        assert cb_mock.call_count == 1

    @pytest.mark.it("Allows any BaseExceptions raised in callback to propagate")
    def test_callback_raises_base_exception(self, mocker, arbitrary_base_exception):
        manager = OperationManager()
        mid = 1
        cb_mock = mocker.MagicMock(side_effect=arbitrary_base_exception)

        # Cause early completion of an unknown operation
        manager.complete_operation(mid)

        # Establish operation that was already completed
        with pytest.raises(arbitrary_base_exception.__class__) as e_info:
            manager.establish_operation(mid, cb_mock)
        assert e_info.value is arbitrary_base_exception

    @pytest.mark.it("Does not trigger the callback until after thread lock has been released")
    def test_callback_called_after_lock_release(self, mocker):
        manager = OperationManager()
        mid = 1
        cb_mock = mocker.MagicMock()

        # Cause early completion of an unknown operation
        manager.complete_operation(mid)

        # Set up mock tracking
        lock_spy = mocker.spy(manager, "_lock")
        mock_tracker = mocker.MagicMock()
        calls_during_lock = []

        # When the lock enters, start recording calls to callback
        # When the lock exits, copy the list of calls.

        def track_mocks():
            mock_tracker.attach_mock(cb_mock, "cb")

        def stop_tracking_mocks(*args):
            local_calls_during_lock = calls_during_lock  # do this for python2 compat
            local_calls_during_lock += copy.copy(mock_tracker.mock_calls)
            mock_tracker.reset_mock()

        lock_spy.__enter__.side_effect = track_mocks
        lock_spy.__exit__.side_effect = stop_tracking_mocks

        # Establish operation that was already completed
        manager.establish_operation(mid, cb_mock)

        # Callback WAS called, but...
        assert cb_mock.call_count == 1

        # Callback WAS NOT called while the lock was held
        assert mocker.call.cb() not in calls_during_lock


@pytest.mark.describe("OperationManager - .complete_operation()")
class TestOperationManagerCompleteOperation(object):
    @pytest.mark.it("Resolves a operation tracking when MID corresponds to a pending operation")
    def test_complete_pending_operation(self):
        manager = OperationManager()
        mid = 1

        # Establish a pending operation
        manager.establish_operation(mid)
        assert len(manager._pending_operation_callbacks) == 1

        # Complete pending operation
        manager.complete_operation(mid)
        assert len(manager._pending_operation_callbacks) == 0

    @pytest.mark.it("Triggers callback for a pending operation when resolving")
    def test_complete_pending_operation_callback(self, mocker):
        manager = OperationManager()
        mid = 1
        cb_mock = mocker.MagicMock()

        manager.establish_operation(mid, cb_mock)
        assert cb_mock.call_count == 0

        manager.complete_operation(mid)
        assert cb_mock.call_count == 1
        assert cb_mock.call_args == mocker.call()

    @pytest.mark.it("Recovers from Exception thrown in callback")
    def test_callback_raises_exception(self, mocker, arbitrary_exception):
        manager = OperationManager()
        mid = 1
        cb_mock = mocker.MagicMock(side_effect=arbitrary_exception)

        manager.establish_operation(mid, cb_mock)
        assert cb_mock.call_count == 0

        manager.complete_operation(mid)
        # Callback was called but exception did not propagate
        assert cb_mock.call_count == 1

    @pytest.mark.it("Allows any BaseExceptions raised in callback to propagate")
    def test_callback_raises_base_exception(self, mocker, arbitrary_base_exception):
        manager = OperationManager()
        mid = 1
        cb_mock = mocker.MagicMock(side_effect=arbitrary_base_exception)

        manager.establish_operation(mid, cb_mock)
        assert cb_mock.call_count == 0

        with pytest.raises(arbitrary_base_exception.__class__) as e_info:
            manager.complete_operation(mid)
        assert e_info.value is arbitrary_base_exception

    @pytest.mark.it(
        "Begins tracking an unknown completion if MID does not correspond to a pending operation"
    )
    def test_early_completion(self):
        manager = OperationManager()
        mid = 1

        manager.complete_operation(mid)
        assert len(manager._unknown_operation_completions) == 1
        assert manager._unknown_operation_completions[mid]

    @pytest.mark.it("Does not trigger the callback until after thread lock has been released")
    def test_callback_called_after_lock_release(self, mocker):
        manager = OperationManager()
        mid = 1
        cb_mock = mocker.MagicMock()

        # Set up an operation and save the callback
        manager.establish_operation(mid, cb_mock)

        # Set up mock tracking
        lock_spy = mocker.spy(manager, "_lock")
        mock_tracker = mocker.MagicMock()
        calls_during_lock = []

        # When the lock enters, start recording calls to callback
        # When the lock exits, copy the list of calls.

        def track_mocks():
            mock_tracker.attach_mock(cb_mock, "cb")

        def stop_tracking_mocks(*args):
            local_calls_during_lock = calls_during_lock  # do this for python2 compat
            local_calls_during_lock += copy.copy(mock_tracker.mock_calls)
            mock_tracker.reset_mock()

        lock_spy.__enter__.side_effect = track_mocks
        lock_spy.__exit__.side_effect = stop_tracking_mocks

        # Complete the operation
        manager.complete_operation(mid)

        # Callback WAS called, but...
        assert cb_mock.call_count == 1
        assert cb_mock.call_args == mocker.call()

        # Callback WAS NOT called while the lock was held
        assert mocker.call.cb() not in calls_during_lock


@pytest.mark.describe("OperationManager - .cancel_all_operations()")
class TestOperationManagerCancelAllOperations(object):
    @pytest.mark.it("Removes all MID tracking for all pending operations")
    def test_remove_pending_ops(self):
        manager = OperationManager()

        # Establish pending operations
        manager.establish_operation(mid=1)
        manager.establish_operation(mid=2)
        manager.establish_operation(mid=3)
        assert len(manager._pending_operation_callbacks) == 3

        # Cancel operations
        manager.cancel_all_operations()
        assert len(manager._pending_operation_callbacks) == 0

    @pytest.mark.it("Removes all MID tracking for unknown operation completions")
    def test_remove_unknown_completions(self):
        manager = OperationManager()

        # Add unknown operation completions
        manager.complete_operation(mid=2111)
        manager.complete_operation(mid=30045)
        manager.complete_operation(mid=2345)
        assert len(manager._unknown_operation_completions) == 3

        # Cancel operations
        manager.cancel_all_operations()
        assert len(manager._unknown_operation_completions) == 0

    @pytest.mark.it("Triggers callbacks (if present) with cancel flag for each pending operation")
    def test_op_callback_completion(self, mocker):
        manager = OperationManager()

        # Establish pending operations
        cb_mock1 = mocker.MagicMock()
        manager.establish_operation(mid=1, callback=cb_mock1)
        cb_mock2 = mocker.MagicMock()
        manager.establish_operation(mid=2, callback=cb_mock2)
        manager.establish_operation(mid=3, callback=None)
        assert cb_mock1.call_count == 0
        assert cb_mock2.call_count == 0

        # Cancel operations
        manager.cancel_all_operations()
        assert cb_mock1.call_count == 1
        assert cb_mock1.call_args == mocker.call(cancelled=True)
        assert cb_mock2.call_count == 1
        assert cb_mock2.call_args == mocker.call(cancelled=True)

    @pytest.mark.it("Recovers from Exception thrown in callback")
    def test_callback_raises_exception(self, mocker, arbitrary_exception):
        manager = OperationManager()

        # Establish pending operation
        cb_mock = mocker.MagicMock(side_effect=arbitrary_exception)
        manager.establish_operation(mid=1, callback=cb_mock)
        assert cb_mock.call_count == 0

        # Cancel operations
        manager.cancel_all_operations()

        # Callback was called but exception did not propagate
        assert cb_mock.call_count == 1

    @pytest.mark.it("Allows any BaseExceptions raised in callback to propagate")
    def test_callback_raises_base_exception(self, mocker, arbitrary_base_exception):
        manager = OperationManager()

        # Establish pending operation
        cb_mock = mocker.MagicMock(side_effect=arbitrary_base_exception)
        manager.establish_operation(mid=1, callback=cb_mock)
        assert cb_mock.call_count == 0

        # When cancelling operations, Base Exception propagates
        with pytest.raises(arbitrary_base_exception.__class__) as e_info:
            manager.cancel_all_operations()
        assert e_info.value is arbitrary_base_exception

    @pytest.mark.it("Does not trigger callbacks until after thread lock has been released")
    def test_callback_called_after_lock_release(self, mocker):
        manager = OperationManager()
        cb_mock1 = mocker.MagicMock()
        cb_mock2 = mocker.MagicMock()

        # Set up operations and save the callback
        manager.establish_operation(mid=1, callback=cb_mock1)
        manager.establish_operation(mid=2, callback=cb_mock2)

        # Set up mock tracking
        lock_spy = mocker.spy(manager, "_lock")
        mock_tracker = mocker.MagicMock()
        calls_during_lock = []

        # When the lock enters, start recording calls to callback
        # When the lock exits, copy the list of calls.

        def track_mocks():
            mock_tracker.attach_mock(cb_mock1, "cb1")
            mock_tracker.attach_mock(cb_mock2, "cb2")

        def stop_tracking_mocks(*args):
            local_calls_during_lock = calls_during_lock  # do this for python2 compat
            local_calls_during_lock += copy.copy(mock_tracker.mock_calls)
            mock_tracker.reset_mock()

        lock_spy.__enter__.side_effect = track_mocks
        lock_spy.__exit__.side_effect = stop_tracking_mocks

        # Cancel operations
        manager.cancel_all_operations()

        # Callbacks WERE called, but...
        assert cb_mock1.call_count == 1
        assert cb_mock1.call_args == mocker.call(cancelled=True)
        assert cb_mock2.call_count == 1
        assert cb_mock2.call_args == mocker.call(cancelled=True)

        # Callbacks WERE NOT called while the lock was held
        assert mocker.call.cb1() not in calls_during_lock
        assert mocker.call.cb2() not in calls_during_lock
