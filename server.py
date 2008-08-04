#!/usr/bin/python
# -*- mode: python; coding: utf-8 -*-
# 
# Mandos server - give out binary blobs to connecting clients.
# 
# This program is partly derived from an example program for an Avahi
# service publisher, downloaded from
# <http://avahi.org/wiki/PythonPublishExample>.  This includes the
# methods "add" and "remove" in the "AvahiService" class, the
# "server_state_changed" and "entry_group_state_changed" functions,
# and some lines in "main".
# 
# Everything else is
# Copyright © 2007-2008 Teddy Hogeborn & Björn Påhlsson
# 
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
#     This program is distributed in the hope that it will be useful,
#     but WITHOUT ANY WARRANTY; without even the implied warranty of
#     MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#     GNU General Public License for more details.
# 
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
# 
# Contact the authors at <mandos@fukt.bsnet.se>.
# 

from __future__ import division

import SocketServer
import socket
import select
from optparse import OptionParser
import datetime
import errno
import gnutls.crypto
import gnutls.connection
import gnutls.errors
import gnutls.library.functions
import gnutls.library.constants
import gnutls.library.types
import ConfigParser
import sys
import re
import os
import signal
from sets import Set
import subprocess
import atexit
import stat
import logging
import logging.handlers

import dbus
import gobject
import avahi
from dbus.mainloop.glib import DBusGMainLoop
import ctypes


logger = logging.Logger('mandos')
syslogger = logging.handlers.SysLogHandler\
            (facility = logging.handlers.SysLogHandler.LOG_DAEMON)
syslogger.setFormatter(logging.Formatter\
                        ('%(levelname)s: %(message)s'))
logger.addHandler(syslogger)
del syslogger


class AvahiError(Exception):
    def __init__(self, value):
        self.value = value
    def __str__(self):
        return repr(self.value)

class AvahiServiceError(AvahiError):
    pass

class AvahiGroupError(AvahiError):
    pass


class AvahiService(object):
    """An Avahi (Zeroconf) service.
    Attributes:
    interface: integer; avahi.IF_UNSPEC or an interface index.
               Used to optionally bind to the specified interface.
    name: string; Example: 'Mandos'
    type: string; Example: '_mandos._tcp'.
                  See <http://www.dns-sd.org/ServiceTypes.html>
    port: integer; what port to announce
    TXT: list of strings; TXT record for the service
    domain: string; Domain to publish on, default to .local if empty.
    host: string; Host to publish records for, default is localhost
    max_renames: integer; maximum number of renames
    rename_count: integer; counter so we only rename after collisions
                  a sensible number of times
    """
    def __init__(self, interface = avahi.IF_UNSPEC, name = None,
                 type = None, port = None, TXT = None, domain = "",
                 host = "", max_renames = 12):
        self.interface = interface
        self.name = name
        self.type = type
        self.port = port
        if TXT is None:
            self.TXT = []
        else:
            self.TXT = TXT
        self.domain = domain
        self.host = host
        self.rename_count = 0
    def rename(self):
        """Derived from the Avahi example code"""
        if self.rename_count >= self.max_renames:
            logger.critical(u"No suitable service name found after %i"
                            u" retries, exiting.", rename_count)
            raise AvahiServiceError("Too many renames")
        name = server.GetAlternativeServiceName(name)
        logger.error(u"Changing name to %r ...", name)
        self.remove()
        self.add()
        self.rename_count += 1
    def remove(self):
        """Derived from the Avahi example code"""
        if group is not None:
            group.Reset()
    def add(self):
        """Derived from the Avahi example code"""
        global group
        if group is None:
            group = dbus.Interface\
                    (bus.get_object(avahi.DBUS_NAME,
                                    server.EntryGroupNew()),
                     avahi.DBUS_INTERFACE_ENTRY_GROUP)
            group.connect_to_signal('StateChanged',
                                    entry_group_state_changed)
        logger.debug(u"Adding service '%s' of type '%s' ...",
                     service.name, service.type)
        group.AddService(
                self.interface,         # interface
                avahi.PROTO_INET6,      # protocol
                dbus.UInt32(0),         # flags
                self.name, self.type,
                self.domain, self.host,
                dbus.UInt16(self.port),
                avahi.string_array_to_txt_array(self.TXT))
        group.Commit()

# From the Avahi example code:
group = None                            # our entry group
# End of Avahi example code


class Client(object):
    """A representation of a client host served by this server.
    Attributes:
    name:      string; from the config file, used in log messages
    fingerprint: string (40 or 32 hexadecimal digits); used to
                 uniquely identify the client
    secret:    bytestring; sent verbatim (over TLS) to client
    fqdn:      string (FQDN); available for use by the checker command
    created:   datetime.datetime(); object creation, not client host
    last_checked_ok: datetime.datetime() or None if not yet checked OK
    timeout:   datetime.timedelta(); How long from last_checked_ok
                                     until this client is invalid
    interval:  datetime.timedelta(); How often to start a new checker
    stop_hook: If set, called by stop() as stop_hook(self)
    checker:   subprocess.Popen(); a running checker process used
                                   to see if the client lives.
                                   'None' if no process is running.
    checker_initiator_tag: a gobject event source tag, or None
    stop_initiator_tag:    - '' -
    checker_callback_tag:  - '' -
    checker_command: string; External command which is run to check if
                     client lives.  %() expansions are done at
                     runtime with vars(self) as dict, so that for
                     instance %(name)s can be used in the command.
    Private attibutes:
    _timeout: Real variable for 'timeout'
    _interval: Real variable for 'interval'
    _timeout_milliseconds: Used when calling gobject.timeout_add()
    _interval_milliseconds: - '' -
    """
    def _set_timeout(self, timeout):
        "Setter function for 'timeout' attribute"
        self._timeout = timeout
        self._timeout_milliseconds = ((self.timeout.days
                                       * 24 * 60 * 60 * 1000)
                                      + (self.timeout.seconds * 1000)
                                      + (self.timeout.microseconds
                                         // 1000))
    timeout = property(lambda self: self._timeout,
                       _set_timeout)
    del _set_timeout
    def _set_interval(self, interval):
        "Setter function for 'interval' attribute"
        self._interval = interval
        self._interval_milliseconds = ((self.interval.days
                                        * 24 * 60 * 60 * 1000)
                                       + (self.interval.seconds
                                          * 1000)
                                       + (self.interval.microseconds
                                          // 1000))
    interval = property(lambda self: self._interval,
                        _set_interval)
    del _set_interval
    def __init__(self, name = None, stop_hook=None, config={}):
        """Note: the 'checker' key in 'config' sets the
        'checker_command' attribute and *not* the 'checker'
        attribute."""
        self.name = name
        logger.debug(u"Creating client %r", self.name)
        # Uppercase and remove spaces from fingerprint for later
        # comparison purposes with return value from the fingerprint()
        # function
        self.fingerprint = config["fingerprint"].upper()\
                           .replace(u" ", u"")
        logger.debug(u"  Fingerprint: %s", self.fingerprint)
        if "secret" in config:
            self.secret = config["secret"].decode(u"base64")
        elif "secfile" in config:
            sf = open(config["secfile"])
            self.secret = sf.read()
            sf.close()
        else:
            raise TypeError(u"No secret or secfile for client %s"
                            % self.name)
        self.fqdn = config.get("fqdn", "")
        self.created = datetime.datetime.now()
        self.last_checked_ok = None
        self.timeout = string_to_delta(config["timeout"])
        self.interval = string_to_delta(config["interval"])
        self.stop_hook = stop_hook
        self.checker = None
        self.checker_initiator_tag = None
        self.stop_initiator_tag = None
        self.checker_callback_tag = None
        self.check_command = config["checker"]
    def start(self):
        """Start this client's checker and timeout hooks"""
        # Schedule a new checker to be started an 'interval' from now,
        # and every interval from then on.
        self.checker_initiator_tag = gobject.timeout_add\
                                     (self._interval_milliseconds,
                                      self.start_checker)
        # Also start a new checker *right now*.
        self.start_checker()
        # Schedule a stop() when 'timeout' has passed
        self.stop_initiator_tag = gobject.timeout_add\
                                  (self._timeout_milliseconds,
                                   self.stop)
    def stop(self):
        """Stop this client.
        The possibility that a client might be restarted is left open,
        but not currently used."""
        # If this client doesn't have a secret, it is already stopped.
        if self.secret:
            logger.info(u"Stopping client %s", self.name)
            self.secret = None
        else:
            return False
        if getattr(self, "stop_initiator_tag", False):
            gobject.source_remove(self.stop_initiator_tag)
            self.stop_initiator_tag = None
        if getattr(self, "checker_initiator_tag", False):
            gobject.source_remove(self.checker_initiator_tag)
            self.checker_initiator_tag = None
        self.stop_checker()
        if self.stop_hook:
            self.stop_hook(self)
        # Do not run this again if called by a gobject.timeout_add
        return False
    def __del__(self):
        self.stop_hook = None
        self.stop()
    def checker_callback(self, pid, condition):
        """The checker has completed, so take appropriate actions."""
        now = datetime.datetime.now()
        self.checker_callback_tag = None
        self.checker = None
        if os.WIFEXITED(condition) \
               and (os.WEXITSTATUS(condition) == 0):
            logger.info(u"Checker for %(name)s succeeded",
                        vars(self))
            self.last_checked_ok = now
            gobject.source_remove(self.stop_initiator_tag)
            self.stop_initiator_tag = gobject.timeout_add\
                                      (self._timeout_milliseconds,
                                       self.stop)
        elif not os.WIFEXITED(condition):
            logger.warning(u"Checker for %(name)s crashed?",
                           vars(self))
        else:
            logger.info(u"Checker for %(name)s failed",
                        vars(self))
    def start_checker(self):
        """Start a new checker subprocess if one is not running.
        If a checker already exists, leave it running and do
        nothing."""
        # The reason for not killing a running checker is that if we
        # did that, then if a checker (for some reason) started
        # running slowly and taking more than 'interval' time, the
        # client would inevitably timeout, since no checker would get
        # a chance to run to completion.  If we instead leave running
        # checkers alone, the checker would have to take more time
        # than 'timeout' for the client to be declared invalid, which
        # is as it should be.
        if self.checker is None:
            try:
                # In case check_command has exactly one % operator
                command = self.check_command % self.fqdn
            except TypeError:
                # Escape attributes for the shell
                escaped_attrs = dict((key, re.escape(str(val)))
                                     for key, val in
                                     vars(self).iteritems())
                try:
                    command = self.check_command % escaped_attrs
                except TypeError, error:
                    logger.error(u'Could not format string "%s":'
                                 u' %s', self.check_command, error)
                    return True # Try again later
            try:
                logger.info(u"Starting checker %r for %s",
                            command, self.name)
                self.checker = subprocess.Popen(command,
                                                close_fds=True,
                                                shell=True, cwd="/")
                self.checker_callback_tag = gobject.child_watch_add\
                                            (self.checker.pid,
                                             self.checker_callback)
            except subprocess.OSError, error:
                logger.error(u"Failed to start subprocess: %s",
                             error)
        # Re-run this periodically if run by gobject.timeout_add
        return True
    def stop_checker(self):
        """Force the checker process, if any, to stop."""
        if self.checker_callback_tag:
            gobject.source_remove(self.checker_callback_tag)
            self.checker_callback_tag = None
        if getattr(self, "checker", None) is None:
            return
        logger.debug("Stopping checker for %(name)s", vars(self))
        try:
            os.kill(self.checker.pid, signal.SIGTERM)
            #os.sleep(0.5)
            #if self.checker.poll() is None:
            #    os.kill(self.checker.pid, signal.SIGKILL)
        except OSError, error:
            if error.errno != errno.ESRCH: # No such process
                raise
        self.checker = None
    def still_valid(self):
        """Has the timeout not yet passed for this client?"""
        now = datetime.datetime.now()
        if self.last_checked_ok is None:
            return now < (self.created + self.timeout)
        else:
            return now < (self.last_checked_ok + self.timeout)


def peer_certificate(session):
    "Return the peer's OpenPGP certificate as a bytestring"
    # If not an OpenPGP certificate...
    if gnutls.library.functions.gnutls_certificate_type_get\
            (session._c_object) \
           != gnutls.library.constants.GNUTLS_CRT_OPENPGP:
        # ...do the normal thing
        return session.peer_certificate
    list_size = ctypes.c_uint()
    cert_list = gnutls.library.functions.gnutls_certificate_get_peers\
        (session._c_object, ctypes.byref(list_size))
    if list_size.value == 0:
        return None
    cert = cert_list[0]
    return ctypes.string_at(cert.data, cert.size)


def fingerprint(openpgp):
    "Convert an OpenPGP bytestring to a hexdigit fingerprint string"
    # New GnuTLS "datum" with the OpenPGP public key
    datum = gnutls.library.types.gnutls_datum_t\
        (ctypes.cast(ctypes.c_char_p(openpgp),
                     ctypes.POINTER(ctypes.c_ubyte)),
         ctypes.c_uint(len(openpgp)))
    # New empty GnuTLS certificate
    crt = gnutls.library.types.gnutls_openpgp_crt_t()
    gnutls.library.functions.gnutls_openpgp_crt_init\
        (ctypes.byref(crt))
    # Import the OpenPGP public key into the certificate
    gnutls.library.functions.gnutls_openpgp_crt_import\
                    (crt, ctypes.byref(datum),
                     gnutls.library.constants.GNUTLS_OPENPGP_FMT_RAW)
    # New buffer for the fingerprint
    buffer = ctypes.create_string_buffer(20)
    buffer_length = ctypes.c_size_t()
    # Get the fingerprint from the certificate into the buffer
    gnutls.library.functions.gnutls_openpgp_crt_get_fingerprint\
        (crt, ctypes.byref(buffer), ctypes.byref(buffer_length))
    # Deinit the certificate
    gnutls.library.functions.gnutls_openpgp_crt_deinit(crt)
    # Convert the buffer to a Python bytestring
    fpr = ctypes.string_at(buffer, buffer_length.value)
    # Convert the bytestring to hexadecimal notation
    hex_fpr = u''.join(u"%02X" % ord(char) for char in fpr)
    return hex_fpr


class tcp_handler(SocketServer.BaseRequestHandler, object):
    """A TCP request handler class.
    Instantiated by IPv6_TCPServer for each request to handle it.
    Note: This will run in its own forked process."""
    
    def handle(self):
        logger.info(u"TCP connection from: %s",
                     unicode(self.client_address))
        session = gnutls.connection.ClientSession\
                  (self.request, gnutls.connection.X509Credentials())
        
        line = self.request.makefile().readline()
        logger.debug(u"Protocol version: %r", line)
        try:
            if int(line.strip().split()[0]) > 1:
                raise RuntimeError
        except (ValueError, IndexError, RuntimeError), error:
            logger.error(u"Unknown protocol version: %s", error)
            return
        
        # Note: gnutls.connection.X509Credentials is really a generic
        # GnuTLS certificate credentials object so long as no X.509
        # keys are added to it.  Therefore, we can use it here despite
        # using OpenPGP certificates.
        
        #priority = ':'.join(("NONE", "+VERS-TLS1.1", "+AES-256-CBC",
        #                "+SHA1", "+COMP-NULL", "+CTYPE-OPENPGP",
        #                "+DHE-DSS"))
        priority = "NORMAL"             # Fallback default, since this
                                        # MUST be set.
        if self.server.settings["priority"]:
            priority = self.server.settings["priority"]
        gnutls.library.functions.gnutls_priority_set_direct\
            (session._c_object, priority, None);
        
        try:
            session.handshake()
        except gnutls.errors.GNUTLSError, error:
            logger.warning(u"Handshake failed: %s", error)
            # Do not run session.bye() here: the session is not
            # established.  Just abandon the request.
            return
        try:
            fpr = fingerprint(peer_certificate(session))
        except (TypeError, gnutls.errors.GNUTLSError), error:
            logger.warning(u"Bad certificate: %s", error)
            session.bye()
            return
        logger.debug(u"Fingerprint: %s", fpr)
        client = None
        for c in self.server.clients:
            if c.fingerprint == fpr:
                client = c
                break
        if not client:
            logger.warning(u"Client not found for fingerprint: %s",
                           fpr)
            session.bye()
            return
        # Have to check if client.still_valid(), since it is possible
        # that the client timed out while establishing the GnuTLS
        # session.
        if not client.still_valid():
            logger.warning(u"Client %(name)s is invalid",
                           vars(client))
            session.bye()
            return
        sent_size = 0
        while sent_size < len(client.secret):
            sent = session.send(client.secret[sent_size:])
            logger.debug(u"Sent: %d, remaining: %d",
                         sent, len(client.secret)
                         - (sent_size + sent))
            sent_size += sent
        session.bye()


class IPv6_TCPServer(SocketServer.ForkingTCPServer, object):
    """IPv6 TCP server.  Accepts 'None' as address and/or port.
    Attributes:
        settings:       Server settings
        clients:        Set() of Client objects
    """
    address_family = socket.AF_INET6
    def __init__(self, *args, **kwargs):
        if "settings" in kwargs:
            self.settings = kwargs["settings"]
            del kwargs["settings"]
        if "clients" in kwargs:
            self.clients = kwargs["clients"]
            del kwargs["clients"]
        return super(type(self), self).__init__(*args, **kwargs)
    def server_bind(self):
        """This overrides the normal server_bind() function
        to bind to an interface if one was specified, and also NOT to
        bind to an address or port if they were not specified."""
        if self.settings["interface"]:
            # 25 is from /usr/include/asm-i486/socket.h
            SO_BINDTODEVICE = getattr(socket, "SO_BINDTODEVICE", 25)
            try:
                self.socket.setsockopt(socket.SOL_SOCKET,
                                       SO_BINDTODEVICE,
                                       self.settings["interface"])
            except socket.error, error:
                if error[0] == errno.EPERM:
                    logger.error(u"No permission to"
                                 u" bind to interface %s",
                                 self.settings["interface"])
                else:
                    raise error
        # Only bind(2) the socket if we really need to.
        if self.server_address[0] or self.server_address[1]:
            if not self.server_address[0]:
                in6addr_any = "::"
                self.server_address = (in6addr_any,
                                       self.server_address[1])
            elif self.server_address[1] is None:
                self.server_address = (self.server_address[0],
                                       0)
            return super(type(self), self).server_bind()


def string_to_delta(interval):
    """Parse a string and return a datetime.timedelta

    >>> string_to_delta('7d')
    datetime.timedelta(7)
    >>> string_to_delta('60s')
    datetime.timedelta(0, 60)
    >>> string_to_delta('60m')
    datetime.timedelta(0, 3600)
    >>> string_to_delta('24h')
    datetime.timedelta(1)
    >>> string_to_delta(u'1w')
    datetime.timedelta(7)
    """
    try:
        suffix=unicode(interval[-1])
        value=int(interval[:-1])
        if suffix == u"d":
            delta = datetime.timedelta(value)
        elif suffix == u"s":
            delta = datetime.timedelta(0, value)
        elif suffix == u"m":
            delta = datetime.timedelta(0, 0, 0, 0, value)
        elif suffix == u"h":
            delta = datetime.timedelta(0, 0, 0, 0, 0, value)
        elif suffix == u"w":
            delta = datetime.timedelta(0, 0, 0, 0, 0, 0, value)
        else:
            raise ValueError
    except (ValueError, IndexError):
        raise ValueError
    return delta


def server_state_changed(state):
    """Derived from the Avahi example code"""
    if state == avahi.SERVER_COLLISION:
        logger.error(u"Server name collision")
        service.remove()
    elif state == avahi.SERVER_RUNNING:
        service.add()


def entry_group_state_changed(state, error):
    """Derived from the Avahi example code"""
    logger.debug(u"state change: %i", state)
    
    if state == avahi.ENTRY_GROUP_ESTABLISHED:
        logger.debug(u"Service established.")
    elif state == avahi.ENTRY_GROUP_COLLISION:
        logger.warning(u"Service name collision.")
        service.rename()
    elif state == avahi.ENTRY_GROUP_FAILURE:
        logger.critical(u"Error in group state changed %s",
                        unicode(error))
        raise AvahiGroupError("State changed: %s", str(error))

def if_nametoindex(interface):
    """Call the C function if_nametoindex(), or equivalent"""
    global if_nametoindex
    try:
        if "ctypes.util" not in sys.modules:
            import ctypes.util
        if_nametoindex = ctypes.cdll.LoadLibrary\
            (ctypes.util.find_library("c")).if_nametoindex
    except (OSError, AttributeError):
        if "struct" not in sys.modules:
            import struct
        if "fcntl" not in sys.modules:
            import fcntl
        def if_nametoindex(interface):
            "Get an interface index the hard way, i.e. using fcntl()"
            SIOCGIFINDEX = 0x8933  # From /usr/include/linux/sockios.h
            s = socket.socket()
            ifreq = fcntl.ioctl(s, SIOCGIFINDEX,
                                struct.pack("16s16x", interface))
            s.close()
            interface_index = struct.unpack("I", ifreq[16:20])[0]
            return interface_index
    return if_nametoindex(interface)


def daemon(nochdir, noclose):
    """See daemon(3).  Standard BSD Unix function.
    This should really exist as os.daemon, but it doesn't (yet)."""
    if os.fork():
        sys.exit()
    os.setsid()
    if not nochdir:
        os.chdir("/")
    if os.fork():
        sys.exit()
    if not noclose:
        # Close all standard open file descriptors
        null = os.open(os.path.devnull, os.O_NOCTTY | os.O_RDWR)
        if not stat.S_ISCHR(os.fstat(null).st_mode):
            raise OSError(errno.ENODEV,
                          "/dev/null not a character device")
        os.dup2(null, sys.stdin.fileno())
        os.dup2(null, sys.stdout.fileno())
        os.dup2(null, sys.stderr.fileno())
        if null > 2:
            os.close(null)


def main():
    global main_loop_started
    main_loop_started = False
    
    parser = OptionParser()
    parser.add_option("-i", "--interface", type="string",
                      metavar="IF", help="Bind to interface IF")
    parser.add_option("-a", "--address", type="string",
                      help="Address to listen for requests on")
    parser.add_option("-p", "--port", type="int",
                      help="Port number to receive requests on")
    parser.add_option("--check", action="store_true", default=False,
                      help="Run self-test")
    parser.add_option("--debug", action="store_true", default=False,
                      help="Debug mode; run in foreground and log to"
                      " terminal")
    parser.add_option("--priority", type="string", help="GnuTLS"
                      " priority string (see GnuTLS documentation)")
    parser.add_option("--servicename", type="string", metavar="NAME",
                      help="Zeroconf service name")
    parser.add_option("--configdir", type="string",
                      default="/etc/mandos", metavar="DIR",
                      help="Directory to search for configuration"
                      " files")
    (options, args) = parser.parse_args()
    
    if options.check:
        import doctest
        doctest.testmod()
        sys.exit()
    
    # Default values for config file for server-global settings
    server_defaults = { "interface": "",
                        "address": "",
                        "port": "",
                        "debug": "False",
                        "priority":
                        "SECURE256:!CTYPE-X.509:+CTYPE-OPENPGP",
                        "servicename": "Mandos",
                        }
    
    # Parse config file for server-global settings
    server_config = ConfigParser.SafeConfigParser(server_defaults)
    del server_defaults
    server_config.read(os.path.join(options.configdir, "server.conf"))
    server_section = "server"
    # Convert the SafeConfigParser object to a dict
    server_settings = dict(server_config.items(server_section))
    # Use getboolean on the boolean config option
    server_settings["debug"] = server_config.getboolean\
                               (server_section, "debug")
    del server_config
    
    # Override the settings from the config file with command line
    # options, if set.
    for option in ("interface", "address", "port", "debug",
                   "priority", "servicename", "configdir"):
        value = getattr(options, option)
        if value is not None:
            server_settings[option] = value
    del options
    # Now we have our good server settings in "server_settings"
    
    # Parse config file with clients
    client_defaults = { "timeout": "1h",
                        "interval": "5m",
                        "checker": "fping -q -- %%(fqdn)s",
                        }
    client_config = ConfigParser.SafeConfigParser(client_defaults)
    client_config.read(os.path.join(server_settings["configdir"],
                                    "clients.conf"))
    
    global service
    service = AvahiService(name = server_settings["servicename"],
                           type = "_mandos._tcp", );
    if server_settings["interface"]:
        service.interface = if_nametoindex(server_settings["interface"])
    
    global main_loop
    global bus
    global server
    # From the Avahi example code
    DBusGMainLoop(set_as_default=True )
    main_loop = gobject.MainLoop()
    bus = dbus.SystemBus()
    server = dbus.Interface(
            bus.get_object( avahi.DBUS_NAME, avahi.DBUS_PATH_SERVER ),
            avahi.DBUS_INTERFACE_SERVER )
    # End of Avahi example code
    
    debug = server_settings["debug"]
    
    if debug:
        console = logging.StreamHandler()
        # console.setLevel(logging.DEBUG)
        console.setFormatter(logging.Formatter\
                             ('%(levelname)s: %(message)s'))
        logger.addHandler(console)
        del console
    
    clients = Set()
    def remove_from_clients(client):
        clients.remove(client)
        if not clients:
            logger.critical(u"No clients left, exiting")
            sys.exit()
    
    clients.update(Set(Client(name = section,
                              stop_hook = remove_from_clients,
                              config
                              = dict(client_config.items(section)))
                       for section in client_config.sections()))
    
    if not debug:
        daemon(False, False)
    
    def cleanup():
        "Cleanup function; run on exit"
        global group
        # From the Avahi example code
        if not group is None:
            group.Free()
            group = None
        # End of Avahi example code
        
        while clients:
            client = clients.pop()
            client.stop_hook = None
            client.stop()
    
    atexit.register(cleanup)
    
    if not debug:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGHUP, lambda signum, frame: sys.exit())
    signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit())
    
    for client in clients:
        client.start()
    
    tcp_server = IPv6_TCPServer((server_settings["address"],
                                 server_settings["port"]),
                                tcp_handler,
                                settings=server_settings,
                                clients=clients)
    # Find out what port we got
    service.port = tcp_server.socket.getsockname()[1]
    logger.info(u"Now listening on address %r, port %d, flowinfo %d,"
                u" scope_id %d" % tcp_server.socket.getsockname())
    
    #service.interface = tcp_server.socket.getsockname()[3]
    
    try:
        # From the Avahi example code
        server.connect_to_signal("StateChanged", server_state_changed)
        try:
            server_state_changed(server.GetState())
        except dbus.exceptions.DBusException, error:
            logger.critical(u"DBusException: %s", error)
            sys.exit(1)
        # End of Avahi example code
        
        gobject.io_add_watch(tcp_server.fileno(), gobject.IO_IN,
                             lambda *args, **kwargs:
                             tcp_server.handle_request\
                             (*args[2:], **kwargs) or True)
        
        logger.debug("Starting main loop")
        main_loop_started = True
        main_loop.run()
    except AvahiError, error:
        logger.critical(u"AvahiError: %s" + unicode(error))
        sys.exit(1)
    except KeyboardInterrupt:
        if debug:
            print

if __name__ == '__main__':
    main()
