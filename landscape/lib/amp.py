"""Expose the methods of a remote object over AMP."""

from uuid import uuid4
from twisted.internet.defer import Deferred, maybeDeferred
from twisted.internet.protocol import ServerFactory, ReconnectingClientFactory
from twisted.protocols.amp import Argument, String, Command, AMP
from twisted.python.failure import Failure

from landscape.lib.bpickle import loads, dumps, dumps_table


class MethodCallArgument(Argument):
    """A bpickle-compatible argument."""

    def toString(self, inObject):
        """Serialize an argument."""
        return dumps(inObject)

    def fromString(self, inString):
        """Unserialize an argument."""
        return loads(inString)

    @classmethod
    def check(cls, inObject):
        """Check if an argument is serializable."""
        return type(inObject) in dumps_table


class MethodCallError(Exception):
    """Raised when a L{MethodCall} command fails."""


class MethodCall(Command):
    """Call a method on the object exposed by a L{MethodCallProtocol}."""

    arguments = [("method", String()),
                 ("args", MethodCallArgument()),
                 ("kwargs", MethodCallArgument())]

    response = [("result", MethodCallArgument()),
                ("deferred", String(optional=True))]

    errors = {MethodCallError: "METHOD_CALL_ERROR"}


class DeferredResponse(Command):
    """Fire a L{Deferred} associated with an outstanding method call result."""

    arguments = [("uuid", String()),
                 ("result", MethodCallArgument(optional=True)),
                 ("failure", String(optional=True))]
    requiresAnswer = False


class MethodCallServerProtocol(AMP):
    """Expose methods of a local object over AMP.

    The object to be exposed is expected to be the C{object} attribute of our
    protocol factory.

    @cvar methods: The list of exposed object's methods that can be called with
        the protocol. It must be defined by sub-classes.
    """

    methods = []

    @MethodCall.responder
    def receive_method_call(self, method, args, kwargs):
        """Call an object's method with the given arguments.

        If a connected client sends a L{MethodCall} for method C{foo_bar}, then
        the actual method C{foo_bar} of the object associated with the protocol
        will be called with the given C{args} and C{kwargs} and its return
        value delivered back to the client as response to the command.

        @param method: The name of the object's method to call.
        @param args: The arguments to pass to the method.
        @param kwargs: The keywords arguments to pass to the method.
        """
        if not method in self.methods:
            raise MethodCallError("Forbidden method '%s'" % method)

        method_func = getattr(self.factory.object, method)
        result = maybeDeferred(method_func, *args, **kwargs)

        # If the Deferred was already fired, we can return its result
        if result.called:
            if isinstance(result.result, Failure):
                failure = str(result.result.value)
                result.addErrback(lambda error: None) # Stop propagating
                raise MethodCallError(failure)
            return {"result": self._check_result(result.result)}

        uuid = str(uuid4())
        result.addBoth(self.send_deferred_response, uuid)
        return {"result": None, "deferred": uuid}

    def _check_result(self, result):
        """Check that the C{result} we're about to return is serializable.

        @return: The C{result} itself if valid.
        @raises: L{MethodCallError} if C{result} is not serializable.
        """
        if not MethodCallArgument.check(result):
            raise MethodCallError("Non-serializable result")
        return result

    def send_deferred_response(self, result, uuid):
        """Send a L{DeferredResponse} for the deferred with given C{uuid}.

        This is called when the result of a L{Deferred} returned by an
        object's method becomes available. A L{DeferredResponse} notifying
        such result (either success or failure) is sent to the peer.
        """
        kwargs = {"uuid": uuid}
        if isinstance(result, Failure):
            kwargs["failure"] = str(result.value)
        else:
            kwargs["result"] = self._check_result(result)
        self.callRemote(DeferredResponse, **kwargs)


class MethodCallClientProtocol(AMP):
    """Calls methods of a remote object over L{AMP}.

    @note: If the remote method returns a deferred, the associated local
        deferred returned by L{send_method_call} will result in the same
        callback value of the remote deferred.
    @cvar timeout: A timeout for remote methods returning L{Deferred}s, if a
        response for the deferred is not received within this amount of
        seconds, the remote method call will errback with a L{MethodCallError}.
    """
    timeout = 60

    def __init__(self):
        AMP.__init__(self)
        self._pending_responses = {}

    @DeferredResponse.responder
    def receive_deferred_response(self, uuid, result, failure):
        """Receive the deferred L{MethodCall} response.

        @param uuid: The id of the L{MethodCall} we're getting the result of.
        @param result: The result of the associated deferred if successful.
        @param failure: The failure message of the deferred if it failed.
        """
        self.fire_pending_response(uuid, result, failure)
        return {}

    def fire_pending_response(self, uuid, result, failure):
        """Fire the L{Deferred} associated with a pending response.

        @param uuid: The id of the L{MethodCall} we're getting the result of.
        @param result: The result of the associated deferred if successful.
        @param failure: The failure message of the deferred if it failed.
        """
        try:
            deferred, call = self._pending_responses.pop(uuid)
        except KeyError:
            # Late response for a request that has timeout, just ignore it
            return
        if not call.called:
            call.cancel()
        if failure is None:
            deferred.callback({"result": result})
        else:
            deferred.errback(MethodCallError(failure))

    def handle_response(self, response):
        """Handle a L{MethodCall} response.

        If the response is tagged as deferred, it will be queued as pending,
        and a L{Deferred} is returned, which will be fired as soon as the
        final response becomes available, or the timeout is reached.
        """
        if response["deferred"]:
            uuid = response["deferred"]
            deferred = Deferred()
            call = self.factory.reactor.callLater(self.timeout,
                                                  self.fire_pending_response,
                                                  uuid, None, "timeout")
            self._pending_responses[uuid] = (deferred, call)
            return deferred

        return response

    def send_method_call(self, method, args=[], kwargs={}):
        """Send a L{MethodCall} command with the given arguments.

        @param method: The name of the remote method to invoke.
        @param args: The positional arguments to pass to the remote method.
        @param kwargs: The keyword arguments to pass to the remote method.
        """
        result = self.callRemote(MethodCall,
                                 method=method, args=args, kwargs=kwargs)
        # The result can be C{None} only if the requested command is a
        # DeferredResponse, which has requiresAnswer set to False
        if result is not None:
            return result.addCallback(self.handle_response)


class MethodCallServerFactory(ServerFactory):
    """Factory for building L{MethodCallProtocol}s exposing an object."""

    protocol = MethodCallServerProtocol

    def __init__(self, object):
        """
        @param object: The object exposed by the L{MethodCallProtocol}s
            instances created by this factory.
        """
        self.object = object


class MethodCallClientFactory(ReconnectingClientFactory):
    """Factory for building L{AMP} connections to L{MethodCall} servers.

    If the connection fails or is lost the factory will keep retrying to
    establish it.

    @cvar protocol: The factory used to build protocol instances.
    @cvar factor: The time factor by which the delay between two subsequent
        connection retries will decrease.
    """

    protocol = MethodCallClientProtocol
    factor = 1.6180339887498948

    def __init__(self, reactor):
        """
        @param reactor: The reactor used by the created protocols
            to schedule notifications and timeouts.
        """
        self.reactor = reactor
        self._notifiers = []

    def add_notifier(self, notifier):
        """Call the given function on connection, reconnection or giveup.

        @param notifier: A function that will be called when the factory builds
            a new connected protocol or gives up connecting.  It will be passed
            the new protocol instance as argument, or the connectionf failure.
        """
        self._notifiers.append(notifier)

    def remove_notifier(self, notifier):
        """Remove a notifier."""
        self._notifiers.remove(notifier)

    def fire_notifiers(self, *args, **kwargs):
        """Notify all registered notifiers."""
        for notifier in self._notifiers:
            self.reactor.callLater(0, notifier, *args, **kwargs)

    def clientConnectionFailed(self, connector, reason):
        ReconnectingClientFactory.clientConnectionFailed(self, connector,
                                                         reason)
        if self.maxRetries is not None and (self.retries > self.maxRetries):
            self.fire_notifiers(reason) # Give up

    def buildProtocol(self, addr):
        self.resetDelay()
        protocol = self.protocol()
        protocol.factory = self
        self.fire_notifiers(protocol)
        return protocol


class RemoteObject(object):
    """An object able to transparently call methods on a remote object.

    Any method call on a L{RemoteObject} instance will return a L{Deferred}
    resulting in the return value of the same method call performed on
    the remote object exposed by the peer.
    """

    def __init__(self, protocol, retry_on_reconnect=False, timeout=None):
        """
        @param protocol: A reference to a connected L{AMP} protocol instance,
            which will be used to send L{MethodCall} commands.
        @param retry_on_reconnect: If C{True}, this L{RemoteObject} will retry
            to perfom again requests that failed due to a lost connection, as
            soon as a new connection is available.
        @param timeout: A timeout for failed requests, if the L{RemoteObject}
            can't perform them again successfully within this amout of seconds,
            they will errback with a L{MethodCallError}.
        """
        self._protocol = protocol
        self._factory = protocol.factory
        self._reactor = protocol.factory.reactor
        self._retry_on_reconnect = retry_on_reconnect
        self._timeout = timeout
        self._pending_requests = {}
        self._factory.add_notifier(self._handle_reconnect)

    def __getattr__(self, method):
        """Return a function sending a L{MethodCall} for the given C{method}.

        When the created function is called, it sends the an appropriate
        L{MethodCall} to the remote peer passing it the arguments and
        keyword arguments it was called with, and returning a L{Deferred}
        resulting in the L{MethodCall}'s response value.
        """

        def send_method_call(*args, **kwargs):
            result = self._protocol.send_method_call(method=method,
                                                     args=args,
                                                     kwargs=kwargs)
            deferred = Deferred()
            result.addCallback(self._handle_response, deferred)
            result.addErrback(self._handle_failure, method, args, kwargs,
                              deferred)
            return deferred

        return send_method_call

    def _handle_reconnect(self, protocol):
        """Handles a reconnection.

        @param protocol: The newly connected protocol instance.
        """
        self._protocol = protocol
        if self._retry_on_reconnect:
            self._retry()

    def _handle_response(self, response, deferred, call=None):
        """Handles a successful L{MethodCall} response.

        @param response: The L{MethodCall} response.
        @param deferred: The deferred that was returned to the caller.
        @param call: If not C{None}, the scheduled timeout call associated with
            the given deferred.
        """
        result = response["result"]
        if call is not None:
            call.cancel()
        deferred.callback(result)

    def _handle_failure(self, failure, method, args, kwargs, deferred,
                        call=None):
        """Called when a L{MethodCall} command fails.

        If a failure is due to a connection error and if C{retry_on_reconnect}
        is C{True}, we will try to perform the requested L{MethodCall} again
        as soon as a new connection becomes available, giving up after the
        specified C{timeout}, if any.

        @param failure: The L{Failure} raised by the requested L{MethodCall}
        @param name: The method name associated with the failed L{MethodCall}
        @param args: The positional arguments of the failed L{MethodCall}.
        @param kwargs: The keyword arguments of the failed L{MethodCall}.
        @param deferred: The deferred that was returned to the caller.
        @param call: If not C{None}, the scheduled timeout call associated with
            the given deferred.
        """
        is_method_call_error = failure.type is MethodCallError
        dont_retry = self._retry_on_reconnect == False

        if is_method_call_error or dont_retry:
            # This means either that the connection is working, and a
            # MethodCall protocol error occured, or that we gave up
            # trying and raised a timeout. In any case just propagate
            # the error.
            if deferred in self._pending_requests:
                self._pending_requests.pop(deferred)
            if call:
                call.cancel()
            deferred.errback(failure)
            return

        if self._timeout and call is None:
            # This is the first failure for this request, let's schedule a
            # timeout call.
            timeout = Failure(MethodCallError("timeout"))
            call = self._reactor.callLater(self._timeout,
                                           self._handle_failure,
                                           timeout, method, args,
                                           kwargs, deferred=deferred)

        self._pending_requests[deferred] = (method, args, kwargs, call)

    def _retry(self):
        """Try to perform again requests that failed."""

        # We need to copy the requests list before iterating over it, because
        # if we are actually still disconnected, callRemote will return a
        # failed deferred and the _handle_failure errback will be executed
        # synchronously during the loop, modifing the requests list itself.
        requests = self._pending_requests.copy()
        self._pending_requests.clear()

        while requests:
            deferred, (method, args, kwargs, call) = requests.popitem()
            result = self._protocol.send_method_call(method, args, kwargs)
            result.addCallback(self._handle_response,
                               deferred=deferred, call=call)
            result.addErrback(self._handle_failure, method, args, kwargs,
                              deferred=deferred, call=call)


class RemoteObjectCreator(object):
    """Connect to remote objects exposed by a L{MethodCallProtocol}."""

    factory = MethodCallClientFactory
    remote = RemoteObject

    def __init__(self, reactor, socket_path, *args, **kwargs):
        """
        @param reactor: A reactor able to connect to Unix sockets.
        @param socket: The path to the socket we want to connect to.
        @param args: Arguments to passed to the created L{RemoteObject}.
        @param kwargs: Keyword arguments for the created L{RemoteObject}.
        """
        self._socket_path = socket_path
        self._reactor = reactor
        self._args = args
        self._kwargs = kwargs

    def connect(self, max_retries=None):
        """Connect to a remote object exposed by a L{MethodCallProtocol}.

        This method will connect to the socket provided in the constructor
        and return a L{Deferred} resulting in a connected L{RemoteObject}.

        @param max_retries: If not C{None} give up try to connect after this
            amount of times.
        """
        self._connected = Deferred()
        self._factory = self.factory(self._reactor)
        self._factory.maxRetries = max_retries
        self._factory.add_notifier(self._done)
        self._reactor.connectUNIX(self._socket_path, self._factory)
        return self._connected

    def _done(self, result):
        """Called when the connection has been established"""
        self._factory.remove_notifier(self._done)
        if isinstance(result, Failure):
            self._connected.errback(result)
        else:
            self._remote = self.remote(result, *self._args, **self._kwargs)
            self._connected.callback(self._remote)

    def disconnect(self):
        """Disconnect the L{RemoteObject} that we have created."""
        self._factory.stopTrying()
        self._remote._protocol.transport.loseConnection()
