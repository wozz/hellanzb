"""

HellaXMLRPC - The hellanzb XML RPC server and client

(c) Copyright 2005 Philip Jenvey
[See end of file]
"""
import os, textwrap, time, Hellanzb
from time import localtime, strftime
from twisted.internet import reactor
from twisted.internet.error import CannotListenError, ConnectionRefusedError
from twisted.python import log
from twisted.web import xmlrpc, server
from twisted.web.server import Site
from xmlrpclib import Fault
from Hellanzb.Elite import C
from Hellanzb.HellaXMLRPC.xmlrpc import Proxy, XMLRPC # was twisted.web.xmlrpc
from Hellanzb.HellaXMLRPC.HtPasswdAuth import HtPasswdWrapper
from Hellanzb.Logging import LogOutputStream
from Hellanzb.Log import *
from Hellanzb.PostProcessor import PostProcessor
from Hellanzb.Util import archiveName, cmHella, flattenDoc, truncateToMultiLine, TopenTwisted

__id__ = '$Id$'

class HellaXMLRPCServer(XMLRPC):
    """ the hellanzb xml rpc server """
    
    def getChild(self, path, request):
        """ This object generates 404s (Default resource.Resource getChild) with HTTP auth turned
        on, so this was needed. not exactly sure why """
        return self
    
    def secondsToUptime(self, seconds):
        """ convert seconds to a pretty uptime output: 2 days, 19:45 """
        days = int(seconds / (60 * 60 * 24))
        hours = int((seconds - (days * 60 * 60 * 24)) / (60 * 60))
        minutes = int((seconds - (days * 60 * 60 * 24) - (hours * 60 * 60)) / 60)
        msg = ''
        if days == 1:
            msg = '%i day, ' % (days)
        elif days > 1:
            msg = '%i days, ' % (days)
        msg += '%.2i:%.2i' % (hours, minutes)
        return msg

    def xmlrpc_asciiart(self):
        """ Return a random ascii art """
        from Hellanzb.Elite import C
        return C.asciiArt()

    def xmlrpc_aolsay(self):
        """ Return a random aolsay (from Da5id's aolsay.scr) """
        from Hellanzb.Elite import C
        return C.aolSay()
        
    def xmlrpc_cancel(self):
        """ Cancel the current download and move the current NZB to Hellanzb.TEMP_DIR """
        from Hellanzb.Daemon import cancelCurrent
        return cancelCurrent()

    def xmlrpc_clear(self, andCancel = False):
        """ Clear the current nzb queue. Specify True as the second argument to clear anything
        currently downloading as well (like the cancel call) """
        from Hellanzb.Daemon import clearCurrent
        return clearCurrent(andCancel)

    def xmlrpc_continue(self):
        """ Continue the paused download """
        from Hellanzb.Daemon import continueCurrent
        return continueCurrent()

    def xmlrpc_dequeue(self, nzbId):
        """ Remove the NZB with specified ID from the queue """
        from Hellanzb.Daemon import dequeueNZBs
        return dequeueNZBs(nzbId)
    
    def xmlrpc_down(self, nzbId, shift = 1):
        """ Move the NZB with the specified ID down in the queue """
        from Hellanzb.Daemon import moveDown
        return moveDown(nzbId, shift)

    def xmlrpc_enqueue(self, nzbFilename):
        """ Add the specified nzb file to the end of the queue """
        from Hellanzb.Daemon import enqueueNZBs
        reactor.callLater(0, enqueueNZBs, nzbFilename)
        return True

    def xmlrpc_list(self, includeIds = False):
        """ List the current queue. Specify True as the second argument to include the NZB ID in
        the listing """
        from Hellanzb.Daemon import listQueue
        return listQueue(includeIds)

    def xmlrpc_force(self, nzbFilename):
        """ Force hellanzb to begin downloading the specified NZB file immediately -- interrupting
        the current download, if necessary """
        from Hellanzb.Daemon import forceNZB
        reactor.callLater(0, forceNZB, nzbFilename)
        return True

    def xmlrpc_maxrate(self, rate = None):
        """ Set the Hellanzb.MAX_RATE (maximum download rate) value. A value of zero denotes no
        rate """
        if rate == None:
            if Hellanzb.ht.readLimit == None:
                return str(None)
            return str(Hellanzb.ht.readLimit / 1024)
        
        from Hellanzb.Daemon import maxRate
        return maxRate(rate)
    
    def xmlrpc_next(self, nzbFilename):
        """ Add the specified nzb file to the beginning of the queue """
        from Hellanzb.Daemon import enqueueNZBs
        reactor.callLater(0, enqueueNextNZBs, nzbFilename)
        return True

    def xmlrpc_pause(self):
        """ Pause the current download """
        from Hellanzb.Daemon import pauseCurrent
        return pauseCurrent()

    def xmlrpc_process(self, archiveDir, rarPassword = None):
        """ Post process the specified directory. The -p option is preferable -- it will do this
        for you, or use the current process if this xml rpc call fails """
        troll = PostProcessor(archiveDir, rarPassword = rarPassword)
        troll.start()
        return True

    def xmlrpc_shutdown(self):
        """ shutdown hellanzb. will quietly kill any post processing threads that may exist """
        # First allow the processors to die/be killed
        Hellanzb.SHUTDOWN = True
        TopenTwisted.killAll()

        # Shutdown the reactor/alert/alert the ui
        reactor.addSystemEventTrigger('after', 'shutdown', logShutdown, 'RPC shutdown call, exiting..')
        from Hellanzb.Core import shutdown
        reactor.callLater(1, shutdown)
        
        return True
        
    def xmlrpc_status(self, aolsay = False):
        """ Return hellanzb's current status text """
        downloading = 'Currently Downloading: '
        processing = 'Currently Processing: '
        failedProcessing = 'Failed Processing: '
        queued = 'Queued: '

        totalSpeed = 0
        activeClients = 0
        for nsf in Hellanzb.nsfs:
            totalSpeed += nsf.sessionSpeed
            activeClients += len(nsf.activeClients)
        if activeClients:
            totalSpeed = '%.1fKB/s' % (totalSpeed)
        else:
            totalSpeed = 'Idle'

        downloading += self.statusFromList(Hellanzb.queue.currentNZBs(),
                                           lambda nzb : nzb.archiveName, len(downloading))
#                                           lambda nzb : nzb.archiveName + ' [' + totalSpeed + ']')

        Hellanzb.postProcessorLock.acquire()
        processing += self.statusFromList(Hellanzb.postProcessors,
                                          lambda postProcessor : archiveName(postProcessor.dirName),
                                          len(processing))
        Hellanzb.postProcessorLock.release()

        queued += self.statusFromList(Hellanzb.queued_nzbs,
                                      lambda nzb : archiveName(nzb.nzbFileName), len(queued))

        # FIXME: show if any archives failed during processing?
        #f = failedProcessing

        lt = localtime()
        hour = int(strftime('%I', lt))
        now = ' ' + str(hour) + strftime(':%M%p', lt)

        # FIXME: optionally don't show ascii
        # hellanzb version %s

        msg = """
%s  up %s  %s
""".lstrip() % (now, self.secondsToUptime(time.time() - Hellanzb.BEGIN_TIME), totalSpeed)
        
        msg += cmHella()

        if aolsay:
            msg += '\n' + textwrap.fill(C.aolSay(), 80, initial_indent = ' '*5,
                                        subsequent_indent = ' '*5) + '\n\n'

        msg += \
"""
%s
%s
%s
        """.strip() % (downloading, processing, queued)

#        \"\"\".strip() % (Hellanzb.version, self.secondsToUptime(time.time() - Hellanzb.BEGIN_TIME),
                       
        return msg

    def xmlrpc_up(self, nzbId, shift = 1):
        """ Move the NZB with the specified ID up in the queue """
        from Hellanzb.Daemon import moveUp
        return moveUp(nzbId, shift)
    
    def statusFromList(self, alist, statusFunc, indent):
        """ generate a status message from the list of objects, using the specified function for
        formatting """
        status = ''
        if len(alist):
            i = 0
            for item in alist:
                if i:
                    status += ' '*indent
                status += statusFunc(item)
                if i < len(alist) - 1:
                    status += '\n'
                i += 1
        else:
            status += 'None'
        return status

def printResultAndExit(remoteCall, result):
    """ generic xml rpc client call back -- simply print the result as a string and exit """
    info(str(result))
    reactor.stop()

def printListAndExit(remoteCall, result):
    [info(line) for line in result]
    reactor.stop()

def resultMadeItBoolAndExit(remoteCall, result):
    """ generic xml rpc call back for a boolean result """
    if type(result) == bool:
        if result:
            info('Successfully made remote call to hellanzb queue daemon')
        else:
            info('Remote call to hellanzb queue daemon returned False! (there was a problem, see logs for details)')
        reactor.stop()
    else:
        info(str(result))
        reactor.stop()

def errHandler(remoteCall, failure):
    """ generic xml rpc client err back -- handle errors, and possibly spawn a post processor
    thread """
    debug('errHandler, class: ' + str(failure.value.__class__) + ' args: ' + \
          str(failure.value.args), failure)

    err = failure.value
    if isinstance(err, ConnectionRefusedError):

        # By default, post process in the already running hellanzb. otherwise do the work
        # in this current process, and exit
        if remoteCall.funcName == 'process':
            from Hellanzb.Daemon import postProcess
            return postProcess(RemoteCall.options)
        
        info('Unable to connect to XMLRPC server: error: ' + str(err) + '\n' + \
             'the hellanzb queue daemon @ ' + Hellanzb.serverUrl + ' probably isn\'t running')

    elif isinstance(err, ValueError):
        if len(err.args) == 2 and err.args[0] == '401':
            info('Incorrect Hellanzb.XMLRPC_PASSWORD: ' + err.args[1] + ' (XMLRPC server: ' + \
                 Hellanzb.serverUrl + ')')
        elif len(err.args) == 2 and err.args[0] == '405':
            info('XMLRPC server: ' + Hellanzb.serverUrl + ' did not understand command: ' + \
                 remoteCall.funcName + \
                 ' -- this server is probably not the hellanzb XML RPC server!')
        else:
            info('Unexpected XMLRPC problem: ' + str(err))
            
    elif isinstance(err, Fault):
        if err.faultCode == 8001:
            info('Invalid command: ' + remoteCall.funcName + ' (XMLRPC server: ' + Hellanzb.serverUrl + \
                 ')')
        elif err.faultCode == 8002:
            info('Invalid command arguments?: ' + remoteCall.funcName + ' (XMLRPC server: ' + \
                 Hellanzb.serverUrl + ')')
        else:
            info('Unexpected XMLRPC response: ' + getStack(err))
            
    reactor.stop()

class RemoteCallArg:
    REQUIRED, OPTIONAL = range(2)

    def __init__(self, name, type):
        self.name = name
        self.type = type
        
class RemoteCall:
    """ XMLRPC client calls, and their callback info """
    callIndex = {}
    # Calling getattr() on the HellaXMLRPCServer class throws a __provides__ error in
    # python2.4 on OS X (the problem is with zope interface). We need to do this to gather
    # our XMLRPC call doc Strings -- this instance has no use other than allowing us
    # access to those doc strings
    serverInstance = HellaXMLRPCServer()

    # for argument definitions
    
    
    def __init__(self, funcName, callback, errback = errHandler):
        self.funcName = funcName
        self.callbackFunc = callback
        self.errbackFunc = errHandler
        self.args = []

        RemoteCall.callIndex[funcName] = self

    def addRequiredArg(self, argname):
        """ denote this remote call as having the specified required arg. order is determined by
        this or addOptionalArg calls are made """
        self.args.append(RemoteCallArg(argname, RemoteCallArg.REQUIRED))

    def addOptionalArg(self, argname):
        """ denote this remote call as having the specified optional arg. order is determined by
        this or addRequiredArg calls are made """
        self.args.append(RemoteCallArg(argname, RemoteCallArg.OPTIONAL))

    def usage(self):
        """ Return the usage string for this rpc call. This is stolen from the xmlrpc Server's
        function doc string """
        for attrName in dir(HellaXMLRPCServer):
            #attr = getattr(HellaXMLRPCServer, attrName) # dies on python2.4 osx
            attr = getattr(RemoteCall.serverInstance, attrName)
            if callable(attr) and attr.__name__ == 'xmlrpc_' + self.funcName:
                return attr.__doc__
        return None

    def callRemote(self, serverUrl = None, *args):
        """ make the remote function call """
        proxy = Proxy(serverUrl)
        if args == None:
            proxy.callRemote(self.funcName).addCallbacks(self.callback, self.errback)
        else:
            proxy.callRemote(self.funcName, *args).addCallbacks(self.callback, self.errback)

    def callback(self, result):
        """ callback from a successful xml rpc call """
        self.callbackFunc(self, result)
        
    def errback(self, failure):
        """ callback from a failed xml rpc call """
        self.errbackFunc(self, failure)

    def call(serverUrl, funcName, args):
        """ lookup the specified function in our pool of known commands, and call it """

    def callLater(serverUrl, funcName, args):
        try:
            rc = RemoteCall.callIndex[funcName]
        except KeyError:
            raise FatalError('Invalid remote call: ' + funcName)
        if rc == None:
            raise FatalError('Invalid remote call: ' + funcName)

        reactor.callLater(0, rc.callRemote, serverUrl, *args)
    callLater = staticmethod(callLater)

    def argsUsage(self):
        """ generate a usage output for all known args """
        msg = ''
        for arg in self.args:
            if arg.type == RemoteCallArg.REQUIRED:
                msg += ' ' + arg.name
            elif arg.type == RemoteCallArg.OPTIONAL:
                msg += ' [' + arg.name + ']'
        return msg

    def allUsage(indent = '  '):
        """ generate a usage output for all known xml rpc commands """
        msg = ''
        calls = RemoteCall.callIndex.keys()
        calls.sort()
        for name in calls:
            call = RemoteCall.callIndex[name]
            prefix = indent + name + call.argsUsage()
            nextIndent = ' '*(24 - len(prefix))
            prefix += nextIndent
            msg += textwrap.fill(prefix + flattenDoc(call.usage()), 79,
                                 subsequent_indent = ' '*len(prefix))
            msg += '\n'
        return msg
    allUsage = staticmethod(allUsage)

def ensureXMLRPCOptions():
    errors = []
    for opt in ('PASSWORD'):
        if not hasattr(Hellanzb, 'XMLRPC_' + opt):
            errors.append('XMLRPC_' + opt)
            
    if len(errors):
        msg = 'Required XMLRPC options not defined the the config file:'
        for err in errors:
            msg += ' ' + err
        raise FatalError(err)

def initXMLRPCServer():
    """ Start the XML RPC server """
    #ensureXMLRPCOptions()

    hxmlrpcs = HellaXMLRPCServer()
    
    SECURE = True
    try:
        if SECURE:
            secure = HtPasswdWrapper(hxmlrpcs, 'hellanzb', Hellanzb.XMLRPC_PASSWORD, 'hellanzb XML RPC')
            reactor.listenTCP(int(Hellanzb.XMLRPC_PORT), Site(secure))
        else:
            reactor.listenTCP(int(Hellanzb.XMLRPC_PORT), Site(hxmlrpcs))
    except CannotListenError, cle:
        error(str(cle))
        raise FatalError('Cannot bind to XML RPC port, is another hellanzb queue daemon already running?')

def initXMLRPCClient():
    """ initialize the xml rpc client """
    # Aliases to these calls would be nice
    
    # FIXME: force should put what it forced out back into the queue cancel is fine the
    # way it is. if you want to swap what youre dling back into the queue, you have to be
    # forcing anyway
    r = RemoteCall('aolsay', printResultAndExit)
    r = RemoteCall('asciiart', printResultAndExit)
    r = RemoteCall('cancel', resultMadeItBoolAndExit)
    r = RemoteCall('clear', resultMadeItBoolAndExit)
    r = RemoteCall('continue', resultMadeItBoolAndExit)
    r = RemoteCall('dequeue', resultMadeItBoolAndExit)
    r.addRequiredArg('nzbid')
    r = RemoteCall('down', resultMadeItBoolAndExit)
    r.addRequiredArg('nzbid')
    r = RemoteCall('enqueue', resultMadeItBoolAndExit)
    r.addRequiredArg('nzbfile')
    r = RemoteCall('force', resultMadeItBoolAndExit)
    # FIXME
    #r.addRequiredArg('nzbid')
    r.addRequiredArg('nzbfile')
    r = RemoteCall('list', printListAndExit)
    r.addOptionalArg('showids')
    r = RemoteCall('maxrate', resultMadeItBoolAndExit)
    r.addOptionalArg('newrate')
    r = RemoteCall('next', resultMadeItBoolAndExit)
    #r.addRequiredArg('nzbid')
    r.addRequiredArg('nzbfile')
    r = RemoteCall('pause', resultMadeItBoolAndExit)
    r = RemoteCall('process', resultMadeItBoolAndExit)
    r.addRequiredArg('archivedir')
    r = RemoteCall('shutdown', resultMadeItBoolAndExit)
    r = RemoteCall('status', printResultAndExit)
    r = RemoteCall('up', resultMadeItBoolAndExit)
    r.addRequiredArg('nzbid')

def hellaRemote(options, args):
    """ execute the remote RPC call with the specified cmd line args. args can be None """
    #ensureXMLRPCOptions()
    
    if options.postProcessDir and options.rarPassword:
        args = ['process', options.postProcessDir, options.rarPassword]
    elif options.postProcessDir:
        args = ['process', options.postProcessDir]
    
    if args[0] in ('force', 'process', 'enqueue', 'next'):
        if len(args) > 1:
            # UNIX: os.path.realpath only available on UNIX
            args[1] = os.path.realpath(args[1])

    if Hellanzb.XMLRPC_PORT == None:
        raise FatalError('Hellanzb.XMLRPC_PORT not defined, cannot make remote call')

    if not hasattr(Hellanzb, 'XMLRPC_SERVER') or Hellanzb.XMLRPC_SERVER == None:
        Hellanzb.XMLRPC_SERVER = 'localhost'
    if Hellanzb.XMLRPC_PASSWORD == None:
        Hellanzb.XMLRPC_PASSWORD == ''
    Hellanzb.serverUrl = 'http://hellanzb:%s@%s:%i' % (Hellanzb.XMLRPC_PASSWORD, Hellanzb.XMLRPC_SERVER,
                                                       Hellanzb.XMLRPC_PORT)

    fileStream = LogOutputStream(debug)
    log.startLogging(fileStream)
    
    funcName = args[0]
    args.remove(funcName)
    RemoteCall.options = options
    RemoteCall.callLater(Hellanzb.serverUrl, funcName, args)
    reactor.run()
    
"""
/*
 * Copyright (c) 2005 Philip Jenvey <pjenvey@groovie.org>
 * All rights reserved.
 *
 * Redistribution and use in source and binary forms, with or without
 * modification, are permitted provided that the following conditions
 * are met:
 * 1. Redistributions of source code must retain the above copyright
 *    notice, this list of conditions and the following disclaimer.
 * 2. Redistributions in binary form must reproduce the above copyright
 *    notice, this list of conditions and the following disclaimer in the
 *    documentation and/or other materials provided with the distribution.
 * 3. The name of the author or contributors may not be used to endorse or
 *    promote products derived from this software without specific prior
 *    written permission.
 *
 * THIS SOFTWARE IS PROVIDED BY THE AUTHOR AND CONTRIBUTORS ``AS IS'' AND
 * ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
 * IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
 * ARE DISCLAIMED.  IN NO EVENT SHALL THE AUTHOR OR CONTRIBUTORS BE LIABLE
 * FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
 * DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS
 * OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
 * HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
 * LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY
 * OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF
 * SUCH DAMAGE.
 *
 * $Id: HellaReactor.py 248 2005-05-03 07:58:12Z pjenvey $
 */
"""
