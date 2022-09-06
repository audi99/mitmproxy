from __future__ import annotations
import asyncio
from dataclasses import dataclass, field
from ssl import VerifyMode
from typing import TYPE_CHECKING, ClassVar

from aioquic.buffer import Buffer as QuicBuffer
from aioquic.h3.connection import ErrorCode as H3ErrorCode
from aioquic.quic import events as quic_events
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.connection import (
    QuicConnection,
    QuicConnectionError,
    QuicConnectionState,
    QuicErrorCode,
    stream_is_client_initiated,
    stream_is_unidirectional,
)
from aioquic.tls import CipherSuite, HandshakeType
from aioquic.quic.packet import (
    PACKET_TYPE_INITIAL,
    QuicHeader,
    QuicProtocolVersion,
    encode_quic_version_negotiation,
    pull_quic_header,
)
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import dsa, ec, rsa
from mitmproxy import certs, connection, flow as mitm_flow, tcp, udp
from mitmproxy.net import tls
from mitmproxy.proxy import commands, context, events, layer
from mitmproxy.proxy.layers.tcp import (
    TcpEndHook,
    TcpErrorHook,
    TcpMessageHook,
    TcpMessageInjected,
    TcpStartHook,
)
from mitmproxy.proxy.layers.tls import (
    TlsClienthelloHook,
    TlsEstablishedClientHook,
    TlsEstablishedServerHook,
    TlsFailedClientHook,
    TlsFailedServerHook,
)
from mitmproxy.proxy.layers.udp import (
    UDPLayer,
    UdpEndHook,
    UdpErrorHook,
    UdpMessageHook,
    UdpMessageInjected,
    UdpStartHook,
)
from mitmproxy.proxy.utils import expect
from mitmproxy.tls import ClientHello, ClientHelloData, TlsData
from mitmproxy.utils import human

if TYPE_CHECKING:
    from mitmproxy.proxy.server import ConnectionHandler


@dataclass
class QuicTlsSettings:
    """
    Settings necessary to establish QUIC's TLS context.
    """

    certificate: x509.Certificate | None = None
    """The certificate to use for the connection."""
    certificate_chain: list[x509.Certificate] = field(default_factory=list)
    """A list of additional certificates to send to the peer."""
    certificate_private_key: dsa.DSAPrivateKey | ec.EllipticCurvePrivateKey | rsa.RSAPrivateKey | None = None
    """The certificate's private key."""
    cipher_suites: list[CipherSuite] | None = None
    """An optional list of allowed/advertised cipher suites."""
    ca_path: str | None = None
    """An optional path to a directory that contains the necessary information to verify the peer certificate."""
    ca_file: str | None = None
    """An optional path to a PEM file that will be used to verify the peer certificate."""
    verify_mode: VerifyMode | None = None
    """An optional flag that specifies how/if the peer's certificate should be validated."""


@dataclass
class QuicTlsData(TlsData):
    """
    Event data for `quic_tls_start_client` and `quic_tls_start_server` event hooks.
    """

    settings: QuicTlsSettings | None = None
    """
    The associated `QuicTlsSettings` object.
    This will be set by an addon in the `quic_tls_start_*` event hooks.
    """


@dataclass
class QuicTlsStartClientHook(commands.StartHook):
    """
    TLS negotiation between mitmproxy and a client over QUIC is about to start.

    An addon is expected to initialize data.settings.
    (by default, this is done by `mitmproxy.addons.tlsconfig`)
    """

    data: QuicTlsData


@dataclass
class QuicTlsStartServerHook(commands.StartHook):
    """
    TLS negotiation between mitmproxy and a server over QUIC is about to start.

    An addon is expected to initialize data.settings.
    (by default, this is done by `mitmproxy.addons.tlsconfig`)
    """

    data: QuicTlsData


@dataclass
class QuicConnectionEvent(events.ConnectionEvent):
    """
    Connection-based event that is triggered whenever a new event from QUIC is received.

    Note:
    'Established' means that an OpenConnection command called in a child layer returned no error.
    Without a predefined child layer, the QUIC layer uses NextLayer mechanics to select the child
    layer. The moment is asks addons for the child layer, the connection is considered established.
    """

    event: quic_events.QuicEvent


class QuicStart(events.DataReceived):
    """
    Event that indicates that QUIC has been established on a given connection.
    This inherits from `DataReceived` in order to trigger next layer behavior and initialize HTTP clients.
    """

    quic: QuicConnection

    def __init__(self, connection: connection.Connection, quic: QuicConnection) -> None:
        super().__init__(connection, data=b"")
        self.quic = quic


class QuicTransmit(commands.ConnectionCommand):
    """Command that will transmit buffered data and re-arm the given QUIC connection's timer."""

    quic: QuicConnection

    def __init__(self, connection: connection.Connection, quic: QuicConnection) -> None:
        super().__init__(connection)
        self.quic = quic


class QuicSecretsLogger:
    logger: tls.MasterSecretLogger

    def __init__(self, logger: tls.MasterSecretLogger) -> None:
        super().__init__()
        self.logger = logger

    def write(self, s: str) -> int:
        if s[-1:] == "\n":
            s = s[:-1]
        data = s.encode("ascii")
        self.logger(None, data)  # type: ignore
        return len(data) + 1

    def flush(self) -> None:
        # done by the logger during write
        pass


def error_code_to_str(error_code: int) -> str:
    """Returns the corresponding name of the given error code or a string containing its numeric value."""

    try:
        return H3ErrorCode(error_code).name
    except ValueError:
        try:
            return QuicErrorCode(error_code).name
        except ValueError:
            return f"unknown error (0x{error_code:x})"


def is_success_error_code(error_code: int) -> bool:
    """Returns whether the given error code actually indicates no error."""

    return error_code in (QuicErrorCode.NO_ERROR, H3ErrorCode.H3_NO_ERROR)


@dataclass
class QuicClientHello(Exception):
    """Helper error only used in `quic_parse_client_hello`."""

    data: bytes


def quic_parse_client_hello(data: bytes) -> ClientHello:
    """Helper function that parses a client hello packet."""

    # ensure the first packet is indeed the initial one
    buffer = QuicBuffer(data=data)
    header = pull_quic_header(buffer)
    if header.packet_type != PACKET_TYPE_INITIAL:
        raise ValueError("Packet is not initial one.")

    # patch aioquic to intercept the client hello
    quic = QuicConnection(
        configuration=QuicConfiguration(
            is_client=False,
            certificate="",
            private_key="",
        ),
        original_destination_connection_id=header.destination_cid,
    )
    _initialize = quic._initialize

    def server_handle_hello_replacement(
        input_buf: QuicBuffer,
        initial_buf: QuicBuffer,
        handshake_buf: QuicBuffer,
        onertt_buf: QuicBuffer,
    ) -> None:
        assert input_buf.pull_uint8() == HandshakeType.CLIENT_HELLO
        length = 0
        for b in input_buf.pull_bytes(3):
            length = (length << 8) | b
        offset = input_buf.tell()
        raise QuicClientHello(input_buf.data_slice(offset, offset + length))

    def initialize_replacement(peer_cid: bytes) -> None:
        try:
            return _initialize(peer_cid)
        finally:
            quic.tls._server_handle_hello = server_handle_hello_replacement

    quic._initialize = initialize_replacement
    try:
        quic.receive_datagram(data, ("0.0.0.0", 0), now=0)
    except QuicClientHello as hello:
        try:
            return ClientHello(hello.data)
        except EOFError as e:
            raise ValueError("Invalid ClientHello data.") from e
    except QuicConnectionError as e:
        raise ValueError(e.reason_phrase) from e
    raise ValueError("No ClientHello returned.")


class QuicStream:
    flow: tcp.TCPFlow | None
    stream_id: int

    def __init__(self, context: context.Context, stream_id: int, ignore: bool) -> None:
        if ignore:
            self.flow = None
        else:
            self.flow = tcp.TCPFlow(context.client, context.server, live=True)
        self.stream_id = stream_id
        is_unidirectional = stream_is_unidirectional(stream_id)
        from_client = stream_is_client_initiated(stream_id)
        self._ended_client = is_unidirectional and not from_client
        self._ended_server = is_unidirectional and from_client

    def has_ended(self, client: bool) -> bool:
        return self._ended_client if client else self._ended_server

    def mark_ended(self, client: bool, err: str | None = None) -> layer.CommandGenerator[None]:
        # ensure we actually change the ended state
        if client:
            assert not self._ended_client
            self._ended_client = True
        else:
            assert not self._ended_server
            self._ended_server = True

        # we're done if the stream is ignored
        if self.flow is None:
            return

        # set and report the first error
        if err is not None and self.flow.error is None:
            self.flow.error = mitm_flow.Error(err)
            yield TcpErrorHook(self.flow)

        # report error-free endings and always clear the live flag
        if self._ended_client and self._ended_server:
            if self.flow.error is None:
                yield TcpEndHook(self.flow)
            self.flow.live = False


class QuicStreamLayer(layer.Layer):
    """
    Layer on top of `ClientQuicLayer` and `ServerQuicLayer`, that simply relays all QUIC streams and datagrams.
    This layer is chosen by the default NextLayer addon if ALPN yields no known protocol.
    It uses `UDPFlow` and `TCPFlow` for datagrams and stream respectively, which makes message injection possible.
    """

    buffer_from_client: list[quic_events.QuicEvent]
    buffer_from_server: list[quic_events.QuicEvent]
    flow: udp.UDPFlow | None  # used for datagrams and to signal general connection issues
    quic_client: QuicConnection | None = None
    quic_server: QuicConnection | None = None
    streams_by_flow: dict[tcp.TCPFlow, QuicStream]
    streams_by_id: dict[int, QuicStream]

    def __init__(self, context: context.Context, ignore: bool = False) -> None:
        super().__init__(context)
        self.buffer_from_client = []
        self.buffer_from_server = []
        if ignore:
            self.flow = None
        else:
            self.flow = udp.UDPFlow(self.context.client, self.context.server, live=True)
        self.streams_by_flow = {}
        self.streams_by_id = {}

    def get_or_create_stream(
        self, stream_id: int
    ) -> layer.CommandGenerator[QuicStream]:
        if stream_id in self.streams_by_id:
            return self.streams_by_id[stream_id]
        else:
            # register the stream and start the flow
            stream = QuicStream(self.context, stream_id, ignore=self.flow is None)
            self.streams_by_id[stream.stream_id] = stream
            if stream.flow is not None:
                self.streams_by_flow[stream.flow] = stream
                yield TcpStartHook(stream.flow)
            return stream

    def handle_quic_event(
        self,
        event: quic_events.QuicEvent,
        from_client: bool,
    ) -> layer.CommandGenerator[None]:
        # buffer events if the peer is not ready yet
        peer_quic = self.quic_server if from_client else self.quic_client
        if peer_quic is None:
            (self.buffer_from_client if from_client else self.buffer_from_server).append(event)
            return
        peer_connection = self.context.server if from_client else self.context.client

        if isinstance(event, quic_events.DatagramFrameReceived):
            # forward datagrams (that are not stream-bound)
            if self.flow is not None:
                udp_message = udp.UDPMessage(from_client, event.data)
                self.flow.messages.append(udp_message)
                yield UdpMessageHook(self.flow)
                data = udp_message.content
            else:
                data = event.data
            peer_quic.send_datagram_frame(data)

        elif isinstance(event, quic_events.StreamDataReceived):
            # ignore data received from already ended streams
            stream = yield from self.get_or_create_stream(event.stream_id)
            if stream.has_ended(from_client):
                yield commands.Log(f"Received {len(event.data)} byte(s) on already closed stream #{event.stream_id}.", level="debug")
                return

            # forward the message allowing addons to change it
            if stream.flow is not None:
                tcp_message = tcp.TCPMessage(from_client, event.data)
                stream.flow.messages.append(tcp_message)
                yield TcpMessageHook(stream.flow)
                data = tcp_message.content
            else:
                data = event.data
            peer_quic.send_stream_data(
                stream.stream_id,
                data,
                event.end_stream,
            )

            # mark the stream as ended if needed
            if event.end_stream:
                yield from stream.mark_ended(from_client)

        elif isinstance(event, quic_events.StreamReset):
            # ignore resets from already ended streams
            stream = yield from self.get_or_create_stream(event.stream_id)
            if stream.has_ended(from_client):
                yield commands.Log(f"Received reset for already closed stream #{event.stream_id}.", level="debug")
                return

            # forward resets to peer streams and report them to addons
            peer_quic.reset_stream(
                stream.stream_id,
                event.error_code,
            )

            # mark the stream as failed
            yield from stream.mark_ended(from_client, err=error_code_to_str(event.error_code))

        else:
            # ignore other QUIC events
            yield commands.Log(f"Ignored QUIC event {event!r}.", level="debug")
            return

        # transmit data to the peer
        yield QuicTransmit(peer_connection, peer_quic)

    @expect(events.Start)
    def state_start(self, _) -> layer.CommandGenerator[None]:
        # mark the main flow as started
        if self.flow is not None:
            yield UdpStartHook(self.flow)

        # open the upstream connection if necessary
        if self.context.server.timestamp_start is None:
            err = yield commands.OpenConnection(self.context.server)
            if err:
                if self.flow is not None:
                    self.flow.error = mitm_flow.Error(str(err))
                    yield UdpErrorHook(self.flow)
                yield commands.CloseConnection(self.context.client)
                self._handle_event = self.state_done
                return
        self._handle_event = self.state_ready

    @expect(
        QuicStart,
        QuicConnectionEvent,
        events.MessageInjected,
        events.ConnectionClosed,
    )
    def state_ready(self, event: events.Event) -> layer.CommandGenerator[None]:

        if isinstance(event, events.ConnectionClosed):
            # define helper variables
            from_client = event.connection is self.context.client
            peer_conn = self.context.server if from_client else self.context.client
            peer_quic = self.quic_server if from_client else self.quic_client
            closed_quic = self.quic_client if from_client else self.quic_server
            close_event = None if closed_quic is None else closed_quic._close_event

            # close the peer as well (needs to be before hooks)
            if peer_quic is not None and close_event is not None:
                peer_quic.close(
                    close_event.error_code,
                    close_event.frame_type,
                    close_event.reason_phrase,
                )
                yield QuicTransmit(peer_conn, peer_quic)
            else:
                yield commands.CloseConnection(peer_conn)

            # report errors to the main flow
            if (
                self.flow is not None
                and close_event is not None
                and not is_success_error_code(close_event.error_code)
            ):
                self.flow.error = mitm_flow.Error(
                    close_event.reason_phrase
                    or error_code_to_str(close_event.error_code)
                )
                yield UdpErrorHook(self.flow)

            # we're done handling QUIC events, pass on to generic close handling
            self._handle_event = self.state_done
            yield from self.state_done(event)

        elif isinstance(event, QuicStart):
            # QUIC connection has been established, store it and get the peer's buffer
            from_client = event.connection is self.context.client
            buffer_from_peer = self.buffer_from_server if from_client else self.buffer_from_client
            if from_client:
                assert self.quic_client is None
                self.quic_client = event.quic
            else:
                assert self.quic_server is None
                self.quic_server = event.quic

            # flush the buffer to the other side
            for quic_event in buffer_from_peer:
                yield from self.handle_quic_event(quic_event, not from_client)
            buffer_from_peer.clear()

        elif isinstance(event, TcpMessageInjected):
            # translate injected TCP messages into QUIC stream events
            assert isinstance(event.flow, tcp.TCPFlow)
            stream = self.streams_by_flow[event.flow]
            yield from self.handle_quic_event(
                quic_events.StreamDataReceived(
                    stream_id=stream.stream_id,
                    data=event.message.content,
                    end_stream=False,
                ),
                event.message.from_client,
            )

        elif isinstance(event, UdpMessageInjected):
            # translate injected UDP messages into QUIC datagram events
            assert isinstance(event.flow, udp.UDPFlow)
            assert event.flow is self.flow
            yield from self.handle_quic_event(
                quic_events.DatagramFrameReceived(data=event.message.content),
                event.message.from_client,
            )

        elif isinstance(event, QuicConnectionEvent):
            # handle or buffer QUIC events
            yield from self.handle_quic_event(
                event.event,
                from_client=event.connection is self.context.client,
            )

        else:
            raise AssertionError(f"Unexpected event: {event!r}")

    @expect(
        QuicStart,
        QuicConnectionEvent,
        events.MessageInjected,
        events.ConnectionClosed,
    )
    def state_done(self, event: events.Event) -> layer.CommandGenerator[None]:
        if isinstance(event, events.ConnectionClosed):
            from_client = event.connection is self.context.client

            # report the termination as error to all non-ended streams
            for stream in self.streams_by_id.values():
                if not stream.has_ended(from_client):
                    yield from stream.mark_ended(from_client, err="Connection closed.")

            # end the main flow
            if (
                self.flow is not None
                and not self.context.client.connected
                and not self.context.server.connected
            ):
                if self.flow.error is None:
                    yield UdpEndHook(self.flow)
                self.flow.live = False

    _handle_event = state_start


class QuicLayer(layer.Layer):
    child_layer: layer.Layer
    conn: connection.Connection
    quic: QuicConnection | None = None
    tls: QuicTlsSettings | None = None

    def __init__(self, context: context.Context, conn: connection.Connection) -> None:
        super().__init__(context)
        self.child_layer = layer.NextLayer(context)
        self.conn = conn
        self._loop = asyncio.get_event_loop()
        self._wakeup_commands: dict[commands.RequestWakeup, float] = dict()
        self._routes: dict[connection.Address, "ConnectionHandler" | None] = dict()
        self.conn.tls = True

    def _handle_event(self, event: events.Event) -> layer.CommandGenerator[None]:
        if (
            isinstance(event, events.DataReceived)
            and event.connection is self.conn
            and self.quic is not None
        ):
            # forward incoming data to aioquic
            self.quic.receive_datagram(event.data, self.conn.peername, now=self._loop.time())
            yield from self.process_events()

        elif (
            isinstance(event, events.ConnectionClosed)
            and event.connection is self.conn
            and self.quic is not None
        ):
            # there is no point in calling quic.close, as it cannot send packets anymore
            # just set the new connection state and ensure there exists a close event
            self.quic._set_state(QuicConnectionState.TERMINATED)
            close_event = self.quic._close_event
            if close_event is None:
                close_event = quic_events.ConnectionTerminated(
                    error_code=QuicErrorCode.APPLICATION_ERROR,
                    frame_type=None,
                    reason_phrase="UDP connection closed or timed out.",
                )
                self.quic._close_event = close_event

            # simulate the termination event and forward close to the child
            yield from self.handle_connection_terminated(close_event)
            yield from self.event_to_child(event)

        elif (
            isinstance(event, events.Wakeup)
            and event.command in self._wakeup_commands
        ):
            # remove the command and handle the timer if we have QUIC
            timer = self._wakeup_commands.pop(event.command)
            if self.quic is not None:
                self.quic.handle_timer(now=max(timer, self._loop.time()))
                yield from self.process_events()

        else:
            # forward other events to the child layer
            yield from self.event_to_child(event)

    def add_route(self, context: context.Context) -> None:
        """Registers a new roamed context."""

        assert context.client.peername not in self._routes
        self._routes[context.client.peername] = context.handler

    def build_configuration(self) -> QuicConfiguration:
        """Creates the aioquic configuration for the current connection."""

        assert self.tls is not None
        return QuicConfiguration(
            alpn_protocols=[offer.decode("ascii") for offer in self.conn.alpn_offers],
            connection_id_length=self.context.options.quic_connection_id_length,
            is_client=self.conn is self.context.server,
            secrets_log_file=QuicSecretsLogger(tls.log_master_secret)  # type: ignore
            if tls.log_master_secret is not None
            else None,
            server_name=self.conn.sni,
            cafile=self.tls.ca_file,
            capath=self.tls.ca_path,
            certificate=self.tls.certificate,
            certificate_chain=self.tls.certificate_chain,
            cipher_suites=self.tls.cipher_suites,
            private_key=self.tls.certificate_private_key,
            verify_mode=self.tls.verify_mode,
        )

    def event_to_child(self, event: events.Event) -> layer.CommandGenerator[None]:
        """Forwards an event to the child layer and handles/intercepts some commands."""

        # filter commands coming from the child layer
        for command in self.child_layer.handle_event(event):

            if (
                isinstance(command, QuicTransmit)
                and command.connection is self.conn
            ):
                # transmit buffered data and re-arm timer
                if command.quic is self.quic:
                    yield from self.transmit()

            elif (
                isinstance(command, commands.CloseConnection)
                and command.connection is self.conn
            ):
                # without QUIC simply close the connection, otherwise close QUIC first
                if self.quic is None:
                    yield command
                else:
                    self.quic.close(reason_phrase="CloseConnection command received.")
                    yield from self.process_events()

            else:
                # return other commands
                yield command

    def handle_connection_id_issued(self, event: quic_events.ConnectionIdIssued) -> layer.CommandGenerator[None]:
        """Called when aioquic issues a new ID for a connection."""

        yield from ()

    def handle_connection_id_retired(self, event: quic_events.ConnectionIdRetired) -> layer.CommandGenerator[None]:
        """Called when aioquic retires an old ID for a connection."""

        yield from ()

    def handle_connection_terminated(self, event: quic_events.ConnectionTerminated) -> layer.CommandGenerator[None]:
        """Called when either the aioquic or underlying connection has been closed."""

        # ensure QUIC has been properly shut down
        assert self.quic is not None
        assert self.tls is not None
        assert self.quic._state is QuicConnectionState.TERMINATED

        # report as TLS failure if the termination happened before the handshake
        reason = event.reason_phrase or error_code_to_str(event.error_code)
        if not self.conn.tls_established:
            self.conn.error = reason
            tls_data = QuicTlsData(self.conn, self.context, settings=self.tls)
            if self.conn is self.context.client:
                yield TlsFailedClientHook(tls_data)
            else:
                yield TlsFailedServerHook(tls_data)

        # clear only quic, tls will indicate we already did `start_tls`
        self.quic = None

        # record an entry in the log
        yield commands.Log(
            f"{self.conn}: QUIC connection destroyed: {reason}",
            level="debug" if is_success_error_code(event.error_code) else "info",
        )

    def handle_handshake_completed(self, event: quic_events.HandshakeCompleted) -> layer.CommandGenerator[None]:
        """Called when aioquic finish the QUIC handshake."""

        # must only be called if QUIC is initialized and not established
        assert self.quic is not None
        assert self.tls is not None
        assert not self.conn.tls_established

        # concatenate all peer certificates
        all_certs: list[x509.Certificate] = []
        if self.quic.tls._peer_certificate is not None:
            all_certs.append(self.quic.tls._peer_certificate)
        if self.quic.tls._peer_certificate_chain is not None:
            all_certs.extend(self.quic.tls._peer_certificate_chain)

        # set the connection's TLS properties
        self.conn.timestamp_tls_setup = self._loop.time()
        self.conn.certificate_list = [certs.Cert(cert) for cert in all_certs]
        self.conn.alpn = event.alpn_protocol.encode("ascii")
        self.conn.cipher = self.quic.tls.key_schedule.cipher_suite.name
        self.conn.tls_version = "QUIC"

        # report the success to addons
        tls_data = QuicTlsData(self.conn, self.context, settings=self.tls)
        if self.conn is self.context.client:
            yield TlsEstablishedClientHook(tls_data)
        else:
            yield TlsEstablishedServerHook(tls_data)

        # record an entry in the log
        yield commands.Log(
            f"{self.conn}: QUIC connection established. "
            f"(early_data={event.early_data_accepted}, resumed={event.session_resumed})",
            level="debug",
        )

    def process_events(self) -> layer.CommandGenerator[None]:
        """
        Retrieves and handles events generated by the aioquic connection.
        This method will also call `transmit`.
        """

        # handle all buffered aioquic connection events
        assert self.quic is not None
        event = self.quic.next_event()
        while event is not None:
            if isinstance(event, quic_events.ConnectionIdIssued):
                yield from self.handle_connection_id_issued(event)
            elif isinstance(event, quic_events.ConnectionIdRetired):
                yield from self.handle_connection_id_retired(event)
            elif isinstance(event, quic_events.ConnectionTerminated):
                yield from self.handle_connection_terminated(event)
                if self.conn.connected:
                    yield commands.CloseConnection(self.conn)  # also close the connection
                return  # we don't handle any further events, nor do/can we transmit data, so exit
            elif isinstance(event, quic_events.HandshakeCompleted):
                yield from self.handle_handshake_completed(event)
                yield from self.event_to_child(QuicStart(self.conn, self.quic))  # notify child layer
            elif isinstance(event, quic_events.PingAcknowledged):
                pass  # we let aioquic do it's thing but don't really care ourselves
            elif isinstance(event, quic_events.ProtocolNegotiated):
                pass  # too early, we act on HandshakeCompleted
            elif isinstance(event, (
                quic_events.DatagramFrameReceived,
                quic_events.StreamDataReceived,
                quic_events.StreamReset,
            )):
                assert self.conn.tls_established  # must be post-handshake event
                yield from self.event_to_child(QuicConnectionEvent(self.conn, event))

            else:
                raise AssertionError(f"Unexpected event: {event!r}")
            event = self.quic.next_event()

        # transmit buffered data and re-arm timer
        yield from self.transmit()

    def remove_route(self, context: context.Context) -> None:
        """Removes a registered roamed context."""

        assert self._routes[context.client.peername] == context.handler
        del self._routes[context.client.peername]

    def start_tls(self, original_destination_connection_id: bytes | None) -> layer.CommandGenerator[bool]:
        """Initiates the aioquic connection."""

        # must only be called if QUIC is uninitialized
        assert self.quic is None
        assert self.tls is None

        # query addons to provide the necessary TLS settings
        tls_data = QuicTlsData(self.conn, self.context)
        if self.conn is self.context.client:
            yield QuicTlsStartClientHook(tls_data)
        else:
            yield QuicTlsStartServerHook(tls_data)
        if tls_data.settings is None:
            yield commands.Log(f"{self.conn}: No QUIC TLS settings provided by addon(s).", level="error")
            return False

        # create the aioquic connection
        self.tls = tls_data.settings
        self.quic = QuicConnection(
            configuration=self.build_configuration(),
            original_destination_connection_id=original_destination_connection_id,
        )

        # issue the host connection ID right away
        self.handle_connection_id_issued(quic_events.ConnectionIdIssued(self.quic.host_cid))

        # record an entry in the log
        yield commands.Log(f"{self.conn}: QUIC connection created.", level="debug")
        return True

    def transmit(self) -> layer.CommandGenerator[None]:
        """Retrieves all pending outgoing packets from aioquic and sends the data."""

        # send all queued datagrams
        assert self.quic is not None
        for data, addr in self.quic.datagrams_to_send(now=self._loop.time()):
            if addr == self.conn.peername:
                yield commands.SendData(self.conn, data)
            else:
                handler = self._routes.get(addr, None)
                if handler is None:
                    yield commands.Log(f"{self.conn}: No route to {human.format_address(addr)}.")
                else:
                    writer = handler.transports[handler.client].writer
                    assert writer is not None
                    writer.write(data)

        # request a new wakeup if all pending requests trigger at a later time
        timer = self.quic.get_timer()
        if not any(existing <= timer for existing in self._wakeup_commands.values()):
            command = commands.RequestWakeup(timer - self._loop.time())
            self._wakeup_commands[command] = timer
            yield command


class ServerQuicLayer(QuicLayer):
    """
    This layer establishes QUIC for a single server connection.
    """

    def __init__(self, context: context.Context) -> None:
        super().__init__(context, context.server)
        self._open_command: commands.OpenConnection | None = None

    def _handle_event(self, event: events.Event) -> layer.CommandGenerator[None]:
        if (
            isinstance(event, events.ConnectionClosed)
            and event.connection is self.conn
            and self._open_command is not None
        ):
            response = events.OpenConnectionCompleted(
                self._open_command,
                (
                    "TLS initialization failed"
                    if self.tls is None else
                    "Connection closed before connect"
                )
            )
            self._open_command = None
            yield from self.event_to_child(response)
        else:
            yield from super()._handle_event(event)

    def event_to_child(self, event: events.Event) -> layer.CommandGenerator[None]:
        for command in super().event_to_child(event):
            if (
                isinstance(command, commands.OpenConnection)
                and command.connection is self.conn
                and self.tls is None
            ):
                # store the command
                assert self._open_command is None
                self._open_command = command

                # try to connect with upstream
                err = yield commands.OpenConnection(self.conn)
                if err:
                    # notify the child layer immediately about the error
                    self._open_command = None
                    yield from self.event_to_child(events.OpenConnectionCompleted(command, err))

                elif not (yield from self.start_tls(None)):
                    # TLS failed, close the connection (notify the child layer once its closed)
                    yield commands.CloseConnection(self.conn)

                else:
                    # connect to server (notify the child layer after handshake)
                    assert self.quic is not None
                    self.quic.connect(self.conn.peername, now=self._loop.time())
                    yield from self.process_events()

            else:
                yield command

    def handle_handshake_completed(self, event: quic_events.HandshakeCompleted) -> layer.CommandGenerator[None]:
        yield from super().handle_handshake_completed(event)

        # notify the child layer that the connection is now open
        if self._open_command is not None:
            command = self._open_command
            self._open_command = None
            yield from self.event_to_child(events.OpenConnectionCompleted(command, None))


class QuicRoamingLayer(layer.Layer):
    """Simple routing layer that replaces a `ClientQuicLayer` when a connection roams to a different `ConnectionHandler`."""

    def __init__(self, context: context.Context, target_layer: ClientQuicLayer) -> None:
        super().__init__(context)
        self.target_layer = target_layer

    @expect()
    def state_closed(self, _) -> layer.CommandGenerator[None]:
        yield from ()

    @expect(events.DataReceived, events.ConnectionClosed, events.MessageInjected)
    def state_relay(self, event: events.Event) -> layer.CommandGenerator[None]:
        if isinstance(event, events.MessageInjected):
            # ensure the flow matches the target and forward the event
            assert event.flow.client_conn is self.target_layer.context.client
            handler = self.target_layer.context.handler
            assert handler
            handler.server_event(event)

        elif isinstance(event, events.ConnectionClosed):
            # remove the registration and stop relaying
            assert event.connection is self.context.client
            self.target_layer.remove_route(self.context)
            self._handle_event = self.state_closed

        elif isinstance(event, events.DataReceived):
            # update target's peername and forward the event
            assert event.connection is self.context.client
            handler = self.target_layer.context.handler
            assert handler
            handler.client.peername = self.context.client.peername
            handler.server_event(events.DataReceived(handler.client, event.data))

        else:
            raise AssertionError(f"Unexpected event: {event!r}")

        yield from ()

    @expect(events.Start)
    def state_start(self, _) -> layer.CommandGenerator[None]:
        # register with the target and start relaying
        self.target_layer.add_route(self.context)
        self._handle_event = self.state_relay
        yield from ()

    _handle_event = state_start


class ClientQuicLayer(QuicLayer):
    """Client-side layer performing routing and connection handling."""

    connections: ClassVar[dict[tuple[connection.Address, bytes], ClientQuicLayer]] = dict()
    """Mapping of (sockname, cid) tuples to QUIC client layers."""

    def __init__(self, context: context.Context, can_roam: bool = False) -> None:
        # same as ClientTLSLayer, we might be nested in some other transport
        if context.client.tls:
            context.client.alpn = None
            context.client.cipher = None
            context.client.sni = None
            context.client.timestamp_tls_setup = None
            context.client.tls_version = None
            context.client.certificate_list = []
            context.client.mitmcert = None
            context.client.alpn_offers = []
            context.client.cipher_list = []
        super().__init__(context, context.client)
        self._can_roam = can_roam
        upper_layer = self.context.layers[-2]
        self._server_layer = upper_layer if isinstance(upper_layer, ServerQuicLayer) else None

    def _handle_event(self, event: events.Event) -> layer.CommandGenerator[None]:
        # try to initialize TLS
        if (
            isinstance(event, events.DataReceived)
            and event.connection is self.conn
            and self.tls is None
        ):
            err = yield from self.datagram_received(event)
            if err:
                yield commands.Log(err)
                return
        yield from super()._handle_event(event)

    def datagram_received(self, event: events.DataReceived) -> layer.CommandGenerator[str | None]:
        # fail if the received data is not a QUIC packet
        buffer = QuicBuffer(data=event.data)
        try:
            header = pull_quic_header(
                buffer, host_cid_length=self.context.options.quic_connection_id_length
            )
        except ValueError:
            return "Invalid QUIC datagram received."

        # negotiate version, support all versions known to aioquic
        supported_versions = (
            version.value
            for version in QuicProtocolVersion
            if version is not QuicProtocolVersion.NEGOTIATION
        )
        if header.version is not None and header.version not in supported_versions:
            yield commands.SendData(
                event.connection,
                encode_quic_version_negotiation(
                    source_cid=header.destination_cid,
                    destination_cid=header.source_cid,
                    supported_versions=supported_versions,
                ),
            )
            return None

        # check if this is a new connection
        target_layer = ClientQuicLayer.connections.get((self.context.client.sockname, header.destination_cid), None)
        if target_layer is None:
            # try to start QUIC
            return (yield from self.start_client_tls(event, header))

        else:
            # ensure that the layer can roam (usually not supported when nested)
            if not self._can_roam:
                return "Connection cannot roam."

            # replace the layer with a roaming layer
            return (yield from self.replace_layer(
                replacement_layer=QuicRoamingLayer(self.context, target_layer),
                first_data_event=event
            ))

    def handle_connection_id_issued(self, event: quic_events.ConnectionIdIssued) -> layer.CommandGenerator[None]:
        connection_id = (self.context.client.sockname, event.connection_id)
        assert connection_id not in ClientQuicLayer.connections
        ClientQuicLayer.connections[connection_id] = self
        yield from super().handle_connection_id_issued(event)

    def handle_connection_id_retired(self, event: quic_events.ConnectionIdRetired) -> layer.CommandGenerator[None]:
        connection_id = (self.context.client.sockname, event.connection_id)
        assert ClientQuicLayer.connections[connection_id] == self
        del ClientQuicLayer.connections[connection_id]
        yield from super().handle_connection_id_retired(event)

    def replace_layer(self, replacement_layer: layer.Layer, first_data_event: events.DataReceived) -> layer.CommandGenerator[str | None]:
        # we need to replace the server layer as well, if there is one
        layer_to_replace = (
            self
            if self._server_layer is None else
            self._server_layer
        )
        layer_to_replace.handle_event = replacement_layer.handle_event  # type:ignore
        layer_to_replace._handle_event = replacement_layer._handle_event  # type:ignore
        yield from replacement_layer.handle_event(events.Start())
        yield from replacement_layer.handle_event(first_data_event)
        return None

    def start_client_tls(self, event: events.DataReceived, header: QuicHeader) -> layer.CommandGenerator[str | None]:
        # ensure it's (likely) a client handshake packet
        if len(event.data) < 1200 or header.packet_type != PACKET_TYPE_INITIAL:
            return "Invalid handshake received."

        # extract the client hello
        try:
            client_hello = quic_parse_client_hello(event.data)
        except ValueError as e:
            return f"Cannot parse ClientHello: {str(e)} ({event.data.hex()})"

        # copy the client hello information
        self.context.client.sni = client_hello.sni
        self.context.client.alpn_offers = client_hello.alpn_protocols

        # check with addons what we shall do
        hook_data = ClientHelloData(self.context, client_hello)
        yield TlsClienthelloHook(hook_data)

        # replace the QUIC layer with an UDP layer if requested
        if hook_data.ignore_connection:
            return (yield from self.replace_layer(
                replacement_layer=UDPLayer(self.context, ignore=True),
                first_data_event=event
            ))

        # start the server QUIC connection if demanded and available
        if (
            hook_data.establish_server_tls_first
            and not self.context.server.tls_established
        ):
            err = (
                (yield commands.OpenConnection(self.context.server))
                if self._server_layer is not None else
                "No server QUIC available."
            )
            if err:
                yield commands.Log(
                    f"Unable to establish QUIC connection with server ({err}). "
                    f"Trying to establish QUIC with client anyway. "
                    f"If you plan to redirect requests away from this server, "
                    f"consider setting `connection_strategy` to `lazy` to suppress early connections."
                )

        # start the client QUIC connection
        if not (yield from self.start_tls(header.destination_cid)):
            return "TLS initialization failed."

        # success
        return None
