# -*- test-case-name: twisted.conch.test.test_unix -*-
# Copyright (c) Twisted Matrix Laboratories.
# See LICENSE for details.

from twisted.cred import checkers, portal
from twisted.python import components, log
from twisted.internet.error import ProcessExitedAlready
from zope import interface
from ssh import session, forwarding, filetransfer
from ssh.filetransfer import FXF_READ, FXF_WRITE, FXF_APPEND, FXF_CREAT, FXF_TRUNC, FXF_EXCL
from twisted.conch.ls import lsLine

from avatar import ConchUser
from error import ConchError
from interfaces import ISession, ISFTPServer, ISFTPFile

import fcntl
import grp
import os
import pwd
import pty
import socket
import struct
import time
import tty
import ttymodes

try:
    import utmp
except ImportError:
    utmp = None



class UnixSSHRealm:
    interface.implements(portal.IRealm)

    def requestAvatar(self, username, mind, *interfaces):
        if username == checkers.ANONYMOUS:
            raise NotImplementedError(
                "Anonymous access for Unix SSH Realm is not yet supported")

        user = UnixConchUser(username)
        return interfaces[0], user, user.logout



class UnixConchUser(ConchUser):

    def __init__(self, username):
        ConchUser.__init__(self)
        self.username = username
        self.pwdData = pwd.getpwnam(self.username)
        l = [self.pwdData[3]]
        for groupname, password, gid, userlist in grp.getgrall():
            if username in userlist:
                l.append(gid)
        self.otherGroups = l
        self.listeners = {}  # dict mapping (interface, port) -> listener
        self.channelLookup.update(
                {"session": session.SSHSession,
                 "direct-tcpip": forwarding.openConnectForwardingClient})

        self.subsystemLookup.update(
                {"sftp": filetransfer.FileTransferServer})

    def getUserGroupId(self):
        return self.pwdData[2:4]

    def getOtherGroups(self):
        return self.otherGroups

    def getHomeDir(self):
        return self.pwdData[5]

    def getShell(self):
        return self.pwdData[6]

    def global_tcpip_forward(self, data):
        hostToBind, portToBind = forwarding.unpackGlobal_tcpip_forward(data)
        from twisted.internet import reactor
        try: listener = self._runAsUser(
                            reactor.listenTCP, portToBind,
                            forwarding.SSHListenForwardingFactory(self.conn,
                                (hostToBind, portToBind),
                                forwarding.SSHListenServerForwardingChannel),
                            interface = hostToBind)
        except:
            return 0
        else:
            self.listeners[(hostToBind, portToBind)] = listener
            if portToBind == 0:
                portToBind = listener.getHost()[2] # the port
                return 1, struct.pack('>L', portToBind)
            else:
                return 1

    def global_cancel_tcpip_forward(self, data):
        hostToBind, portToBind = forwarding.unpackGlobal_tcpip_forward(data)
        listener = self.listeners.get((hostToBind, portToBind), None)
        if not listener:
            return 0
        del self.listeners[(hostToBind, portToBind)]
        self._runAsUser(listener.stopListening)
        return 1

    def logout(self):
        # remove all listeners
        for listener in self.listeners.itervalues():
            self._runAsUser(listener.stopListening)
        log.msg('avatar %s logging out (%i)' % (self.username, len(self.listeners)))

    def _runAsUser(self, f, *args, **kw):
        euid = os.geteuid()
        egid = os.getegid()
        groups = os.getgroups()
        uid, gid = self.getUserGroupId()
        os.setegid(0)
        os.seteuid(0)
        os.setgroups(self.getOtherGroups())
        os.setegid(gid)
        os.seteuid(uid)
        try:
            f = iter(f)
        except TypeError:
            f = [(f, args, kw)]
        try:
            for i in f:
                func = i[0]
                args = len(i)>1 and i[1] or ()
                kw = len(i)>2 and i[2] or {}
                r = func(*args, **kw)
        finally:
            os.setegid(0)
            os.seteuid(0)
            os.setgroups(groups)
            os.setegid(egid)
            os.seteuid(euid)
        return r

class SSHSessionForUnixConchUser:

    interface.implements(ISession)

    def __init__(self, avatar):
        self.avatar = avatar
        self. environ = {'PATH':'/bin:/usr/bin:/usr/local/bin'}
        self.pty = None
        self.ptyTuple = 0

    def addUTMPEntry(self, loggedIn=1):
        if not utmp:
            return
        ipAddress = self.avatar.conn.transport.transport.getPeer().host
        packedIp ,= struct.unpack('L', socket.inet_aton(ipAddress))
        ttyName = self.ptyTuple[2][5:]
        t = time.time()
        t1 = int(t)
        t2 = int((t-t1) * 1e6)
        entry = utmp.UtmpEntry()
        entry.ut_type = loggedIn and utmp.USER_PROCESS or utmp.DEAD_PROCESS
        entry.ut_pid = self.pty.pid
        entry.ut_line = ttyName
        entry.ut_id = ttyName[-4:]
        entry.ut_tv = (t1,t2)
        if loggedIn:
            entry.ut_user = self.avatar.username
            entry.ut_host = socket.gethostbyaddr(ipAddress)[0]
            entry.ut_addr_v6 = (packedIp, 0, 0, 0)
        a = utmp.UtmpRecord(utmp.UTMP_FILE)
        a.pututline(entry)
        a.endutent()
        b = utmp.UtmpRecord(utmp.WTMP_FILE)
        b.pututline(entry)
        b.endutent()


    def getPty(self, term, windowSize, modes):
        self.environ['TERM'] = term
        self.winSize = windowSize
        self.modes = modes
        master, slave = pty.openpty()
        ttyname = os.ttyname(slave)
        self.environ['SSH_TTY'] = ttyname
        self.ptyTuple = (master, slave, ttyname)

    def openShell(self, proto):
        from twisted.internet import reactor
        if not self.ptyTuple: # we didn't get a pty-req
            log.msg('tried to get shell without pty, failing')
            raise ConchError("no pty")
        uid, gid = self.avatar.getUserGroupId()
        homeDir = self.avatar.getHomeDir()
        shell = self.avatar.getShell()
        self.environ['USER'] = self.avatar.username
        self.environ['HOME'] = homeDir
        self.environ['SHELL'] = shell
        shellExec = os.path.basename(shell)
        peer = self.avatar.conn.transport.transport.getPeer()
        host = self.avatar.conn.transport.transport.getHost()
        self.environ['SSH_CLIENT'] = '%s %s %s' % (peer.host, peer.port, host.port)
        self.getPtyOwnership()
        self.pty = reactor.spawnProcess(proto, \
                  shell, ['-%s' % shellExec], self.environ, homeDir, uid, gid,
                   usePTY = self.ptyTuple)
        self.addUTMPEntry()
        fcntl.ioctl(self.pty.fileno(), tty.TIOCSWINSZ,
                    struct.pack('4H', *self.winSize))
        if self.modes:
            self.setModes()
        self.oldWrite = proto.transport.write
        proto.transport.write = self._writeHack
        self.avatar.conn.transport.transport.setTcpNoDelay(1)

    def execCommand(self, proto, cmd):
        from twisted.internet import reactor
        uid, gid = self.avatar.getUserGroupId()
        homeDir = self.avatar.getHomeDir()
        shell = self.avatar.getShell() or '/bin/sh'
        command = (shell, '-c', cmd)
        peer = self.avatar.conn.transport.transport.getPeer()
        host = self.avatar.conn.transport.transport.getHost()
        self.environ['SSH_CLIENT'] = '%s %s %s' % (peer.host, peer.port, host.port)
        if self.ptyTuple:
            self.getPtyOwnership()
        self.pty = reactor.spawnProcess(proto, \
                shell, command, self.environ, homeDir,
                uid, gid, usePTY = self.ptyTuple or 0)
        if self.ptyTuple:
            self.addUTMPEntry()
            if self.modes:
                self.setModes()
#        else:
#            tty.setraw(self.pty.pipes[0].fileno(), tty.TCSANOW)
        self.avatar.conn.transport.transport.setTcpNoDelay(1)

    def getPtyOwnership(self):
        ttyGid = os.stat(self.ptyTuple[2])[5]
        uid, gid = self.avatar.getUserGroupId()
        euid, egid = os.geteuid(), os.getegid()
        os.setegid(0)
        os.seteuid(0)
        try:
            os.chown(self.ptyTuple[2], uid, ttyGid)
        finally:
            os.setegid(egid)
            os.seteuid(euid)

    def setModes(self):
        pty = self.pty
        attr = tty.tcgetattr(pty.fileno())
        for mode, modeValue in self.modes:
            if not ttymodes.TTYMODES.has_key(mode): continue
            ttyMode = ttymodes.TTYMODES[mode]
            if len(ttyMode) == 2: # flag
                flag, ttyAttr = ttyMode
                if not hasattr(tty, ttyAttr): continue
                ttyval = getattr(tty, ttyAttr)
                if modeValue:
                    attr[flag] = attr[flag]|ttyval
                else:
                    attr[flag] = attr[flag]&~ttyval
            elif ttyMode == 'OSPEED':
                attr[tty.OSPEED] = getattr(tty, 'B%s'%modeValue)
            elif ttyMode == 'ISPEED':
                attr[tty.ISPEED] = getattr(tty, 'B%s'%modeValue)
            else:
                if not hasattr(tty, ttyMode): continue
                ttyval = getattr(tty, ttyMode)
                attr[tty.CC][ttyval] = chr(modeValue)
        tty.tcsetattr(pty.fileno(), tty.TCSANOW, attr)

    def eofReceived(self):
        if self.pty:
            self.pty.closeStdin()

    def closed(self):
        if self.ptyTuple and os.path.exists(self.ptyTuple[2]):
            ttyGID = os.stat(self.ptyTuple[2])[5]
            os.chown(self.ptyTuple[2], 0, ttyGID)
        if self.pty:
            try:
                self.pty.signalProcess('HUP')
            except (OSError,ProcessExitedAlready):
                pass
            self.pty.loseConnection()
            self.addUTMPEntry(0)
        log.msg('shell closed')

    def windowChanged(self, winSize):
        self.winSize = winSize
        fcntl.ioctl(self.pty.fileno(), tty.TIOCSWINSZ,
                        struct.pack('4H', *self.winSize))

    def _writeHack(self, data):
        """
        Hack to send ignore messages when we aren't echoing.
        """
        if self.pty is not None:
            attr = tty.tcgetattr(self.pty.fileno())[3]
            if not attr & tty.ECHO and attr & tty.ICANON: # no echo
                self.avatar.conn.transport.sendIgnore('\x00'*(8+len(data)))
        self.oldWrite(data)


class SFTPServerForUnixConchUser:

    interface.implements(ISFTPServer)

    def __init__(self, avatar):
        self.avatar = avatar


    def _setAttrs(self, path, attrs):
        """
        NOTE: this function assumes it runs as the logged-in user:
        i.e. under _runAsUser()
        """
        if "uid" in attrs and "gid" in attrs:
            os.chown(path, attrs["uid"], attrs["gid"])
        if "permissions" in attrs:
            os.chmod(path, attrs["permissions"])
        if "atime" in attrs and "mtime" in attrs:
            os.utime(path, (attrs["atime"], attrs["mtime"]))

    def _getAttrs(self, s):
        return {
            "size" : s.st_size,
            "uid" : s.st_uid,
            "gid" : s.st_gid,
            "permissions" : s.st_mode,
            "atime" : int(s.st_atime),
            "mtime" : int(s.st_mtime)
        }

    def _absPath(self, path):
        home = self.avatar.getHomeDir()
        return os.path.abspath(os.path.join(home, path))

    def gotVersion(self, otherVersion, extData):
        return {}

    def openFile(self, filename, flags, attrs):
        return UnixSFTPFile(self, self._absPath(filename), flags, attrs)

    def removeFile(self, filename):
        filename = self._absPath(filename)
        return self.avatar._runAsUser(os.remove, filename)

    def renameFile(self, oldpath, newpath):
        oldpath = self._absPath(oldpath)
        newpath = self._absPath(newpath)
        return self.avatar._runAsUser(os.rename, oldpath, newpath)

    def makeDirectory(self, path, attrs):
        path = self._absPath(path)
        return self.avatar._runAsUser([(os.mkdir, (path,)),
                                (self._setAttrs, (path, attrs))])

    def removeDirectory(self, path):
        path = self._absPath(path)
        self.avatar._runAsUser(os.rmdir, path)

    def openDirectory(self, path):
        return UnixSFTPDirectory(self, self._absPath(path))

    def getAttrs(self, path, followLinks):
        path = self._absPath(path)
        if followLinks:
            s = self.avatar._runAsUser(os.stat, path)
        else:
            s = self.avatar._runAsUser(os.lstat, path)
        return self._getAttrs(s)

    def setAttrs(self, path, attrs):
        path = self._absPath(path)
        self.avatar._runAsUser(self._setAttrs, path, attrs)

    def readLink(self, path):
        path = self._absPath(path)
        return self.avatar._runAsUser(os.readlink, path)

    def makeLink(self, linkPath, targetPath):
        linkPath = self._absPath(linkPath)
        targetPath = self._absPath(targetPath)
        return self.avatar._runAsUser(os.symlink, targetPath, linkPath)

    def realPath(self, path):
        return os.path.realpath(self._absPath(path))

    def extendedRequest(self, extName, extData):
        raise NotImplementedError

class UnixSFTPFile:

    interface.implements(ISFTPFile)

    def __init__(self, server, filename, flags, attrs):
        self.server = server
        openFlags = 0
        if flags & FXF_READ == FXF_READ and flags & FXF_WRITE == 0:
            openFlags = os.O_RDONLY
        if flags & FXF_WRITE == FXF_WRITE and flags & FXF_READ == 0:
            openFlags = os.O_WRONLY
        if flags & FXF_WRITE == FXF_WRITE and flags & FXF_READ == FXF_READ:
            openFlags = os.O_RDWR
        if flags & FXF_APPEND == FXF_APPEND:
            openFlags |= os.O_APPEND
        if flags & FXF_CREAT == FXF_CREAT:
            openFlags |= os.O_CREAT
        if flags & FXF_TRUNC == FXF_TRUNC:
            openFlags |= os.O_TRUNC
        if flags & FXF_EXCL == FXF_EXCL:
            openFlags |= os.O_EXCL
        if "permissions" in attrs:
            mode = attrs["permissions"]
            del attrs["permissions"]
        else:
            mode = 0777
        fd = server.avatar._runAsUser(os.open, filename, openFlags, mode)
        if attrs:
            server.avatar._runAsUser(server._setAttrs, filename, attrs)
        self.fd = fd

    def close(self):
        return self.server.avatar._runAsUser(os.close, self.fd)

    def readChunk(self, offset, length):
        return self.server.avatar._runAsUser([ (os.lseek, (self.fd, offset, 0)),
                                               (os.read, (self.fd, length)) ])

    def writeChunk(self, offset, data):
        return self.server.avatar._runAsUser([(os.lseek, (self.fd, offset, 0)),
                                       (os.write, (self.fd, data))])

    def getAttrs(self):
        s = self.server.avatar._runAsUser(os.fstat, self.fd)
        return self.server._getAttrs(s)

    def setAttrs(self, attrs):
        raise NotImplementedError


class UnixSFTPDirectory:

    def __init__(self, server, directory):
        self.server = server
        self.files = server.avatar._runAsUser(os.listdir, directory)
        self.dir = directory

    def __iter__(self):
        return self

    def next(self):
        try:
            f = self.files.pop(0)
        except IndexError:
            raise StopIteration
        else:
            s = self.server.avatar._runAsUser(os.lstat, os.path.join(self.dir, f))
            longname = lsLine(f, s)
            attrs = self.server._getAttrs(s)
            return (f, longname, attrs)

    def close(self):
        self.files = []


components.registerAdapter(SFTPServerForUnixConchUser, UnixConchUser, filetransfer.ISFTPServer)
components.registerAdapter(SSHSessionForUnixConchUser, UnixConchUser, session.ISession)
