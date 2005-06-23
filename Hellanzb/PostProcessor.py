"""

PostProcessor (aka troll) - verify/repair/unarchive/decompress files downloaded with
nzbget

(c) Copyright 2005 Philip Jenvey, Ben Bangert
[See end of file]
"""
import os, re, sys, time, Hellanzb
from os.path import join as pathjoin
from shutil import move, rmtree
from threading import Thread, Condition, Lock, RLock
from Hellanzb.Log import *
from Hellanzb.Logging import prettyException
from Hellanzb.PostProcessorUtil import *
from Hellanzb.Util import *

__id__ = '$Id$'

class PostProcessor(Thread):
    """ A post processor (formerly troll) instance runs in its own thread """
    dirName = None
    decompressionThreadPool = None
    decompressorCondition = None
    background = None
    musicFiles = None
    brokenFiles = None

    failedLock = None
    failedToProcesses = None
    
    msgId = None
    nzbFile = None

    def __init__(self, dirName, background = True, rarPassword = None, parentDir = None):
        """ Ensure sanity of this instance before starting """
        # abort if we lack required binaries
        assertIsExe('par2')

         # DirName is a hack printing out the correct directory name when running nested
         # post processors on sub directories
        self.dirName = DirName(dirName)

        # Whether or not this thread is the only thing happening in the app (-p mode)
        self.background = background
        
        # If we're a background Post Processor, our MO is to move dirName to DEST_DIR when
        # finished (successfully or not)
        self.movedDestDir = False
        
        self.decompressionThreadPool = []
        self.decompressorLock = RLock()
        self.decompressorCondition = Condition(self.decompressorLock)

        self.rarPassword = rarPassword
        if self.rarPassword != None:
            rarPasswordFile = open(dirName + os.sep + '.hellanzb_rar_password', 'w')
            rarPasswordFile.write(self.rarPassword)
            rarPasswordFile.close()

        # The parent directory we the originating post process call started from, if we
        # are post processing a sub directory
        self.isSubDir = False
        self.parentDir = parentDir
        if self.parentDir != None:
            self.isSubDir = True
            self.dirName.parentDir = self.parentDir
            
        self.startTime = None
    
        Thread.__init__(self)

    def addDecompressor(self, decompressorThread):
        """ Add a decompressor thread to the pool and notify the caller """
        self.decompressorCondition.acquire()
        self.decompressionThreadPool.append(decompressorThread)
        self.decompressorCondition.notify()
        self.decompressorCondition.release()

    def removeDecompressor(self, decompressorThread):
        """ Remove a decompressor thread from the pool and notify the caller """
        self.decompressorCondition.acquire()
        self.decompressionThreadPool.remove(decompressorThread)
        self.decompressorCondition.notify()
        self.decompressorCondition.release()

    def stop(self):
        """ Perform any cleanup and remove ourself from the pool before exiting """
        cleanUp(self.dirName)

        if not self.isSubDir:
            Hellanzb.postProcessorLock.acquire()
            Hellanzb.postProcessors.remove(self)
            Hellanzb.postProcessorLock.release()
            
        # When a Post Processor fails, we end up moving the destDir here
        self.moveDestDir() 
            
        if not self.background and not self.isSubDir:
            # We're not running in the background of a downloader -- we're post processing
            # and then immeidately exiting (-Lp)
            from twisted.internet import reactor
            reactor.callFromThread(reactor.stop)

    def moveDestDir(self):
        if self.movedDestDir or Hellanzb.SHUTDOWN:
            return
        
        if self.background and not self.isSubDir and \
                os.path.normpath(os.path.dirname(self.dirName.rstrip(os.sep))) == \
                os.path.normpath(Hellanzb.PROCESSING_DIR):

            if os.path.islink(self.dirName):
                # A symlink in the processing dir, remove it
                os.remove(self.dirName)

            elif os.path.isdir(self.dirName):
                # A dir in the processing dir, move it to DEST
                newdir = Hellanzb.DEST_DIR + os.sep + os.path.basename(self.dirName)
                hellaRename(newdir)
                move(self.dirName, newdir)
                
        self.movedDestDir = True
    
    def run(self):
        """ do the work """
        if not self.isSubDir:
            Hellanzb.postProcessorLock.acquire()
            # FIXME: could block if there are too many processors going
            Hellanzb.postProcessors.append(self)
            Hellanzb.postProcessorLock.release()
        
        try:
            self.postProcess()
            
        except SystemExit, se:
            # REACTOR STOPPED IF NOT BACKGROUND/SUBIDR
            self.stop()
            
            if self.isSubDir:
                # Propagate up to the original Post Processor
                raise

            return
        
        except FatalError, fe:
            # REACTOR STOPPED IF NOT BACKGROUND/SUBIDR
            self.stop()

            # Propagate up to the original Post Processor
            if self.isSubDir:
                raise

            pe = prettyException(fe)
            lines = pe.split('\n')
            if self.background and Hellanzb.LOG_FILE and len(lines) > 13:
                # Show only the first 4 and last 4 lines of the error
                begin = ''.join([line + '\n' for line in lines[:3]])
                end = ''.join([line + '\n' for line in lines[-9:]])
                msg = begin + \
                    '\n <hellanzb truncated the error\'s output, see the log file for full output>\n' + \
                    end
            else:
                msg = pe
            
            noLogFile(archiveName(self.dirName) + ': A problem occurred: ' + msg)
            logFile(archiveName(self.dirName) + ': A problem occurred: ', fe)

            return
        
        except Exception, e:
            # REACTOR STOPPED IF NOT BACKGROUND/SUBIDR
            self.stop()
            
            # Propagate up to the original Post Processor
            if self.isSubDir:
                raise
            
            error(archiveName(self.dirName) + ': An unexpected problem occurred', e)

            return

        # REACTOR STOPPED IF NOT BACKGROUND/SUBIDR
        self.stop() # successful post process
    
    def processMusic(self):
        """ Assume the integrity of the files in the specified directory have been
        verified. Iterate through the music files, and decompres them when appropriate in
        multiple threads """
        if not isFreshState(self.dirName, 'music'):
            info(archiveName(self.dirName) + ': Skipping music file decompression')
            return
        
        # Determine the music files to decompress
        self.musicFiles = []
        for file in os.listdir(self.dirName):
            absPath = self.dirName + os.sep + file
            if os.path.isfile(absPath) and getMusicType(file) and getMusicType(file).shouldDecompress():
                self.musicFiles.append(absPath)
    
        if len(self.musicFiles) == 0:
            return

        self.musicFiles.sort()

        threadCount = min(len(self.musicFiles), int(Hellanzb.MAX_DECOMPRESSION_THREADS))
        
        filesTxt = 'file'
        threadsTxt = 'thread'
        if len(self.musicFiles) != 1:
            filesTxt += 's'
        if threadCount != 1:
            threadsTxt += 's'
            
        info(archiveName(self.dirName) + ': Decompressing ' + str(len(self.musicFiles)) + \
             ' ' + filesTxt + ' via ' + str(threadCount) + ' ' + threadsTxt + '..')

        # Failed decompress threads put their file names in this list
        self.failedToProcesses = []
        self.failedLock = Lock()

        # Maintain a pool of threads of the specified size until we've exhausted the
        # musicFiles list
        while len(self.musicFiles) > 0:
    
            # Block the pool until we're done spawning
            self.decompressorCondition.acquire()
            
            if len(self.decompressionThreadPool) < int(Hellanzb.MAX_DECOMPRESSION_THREADS):
                # will pop the next music file off the list
                decompressor = DecompressionThread(parent = self,         
                                                   dirName = self.dirName)
                decompressor.start()
    
            else:
                # Unblock and wait until we're notified of a thread's completition before
                # doing anything else
                self.decompressorCondition.wait()
                
            self.decompressorCondition.release()
            checkShutdown()

        # We're not finished until all the threads are done
        self.decompressorLock.acquire()
        decompressorThreads = self.decompressionThreadPool[:]
        self.decompressorLock.release()
        
        for decompressor in decompressorThreads:
            decompressor.join()

        del decompressorThreads

        if len(self.failedToProcesses) > 0:
            raise FatalError('Failed to complete music decompression')

        processComplete(self.dirName, 'music', None)
        info(archiveName(self.dirName) + ': Finished decompressing')

    def finishedPostProcess(self):
        """ finish the post processing work """
        # Move other cruft out of the way
        deleteDuplicates(self.dirName)
        
        if self.nzbFile != None:
            if os.path.isfile(self.dirName + os.sep + self.nzbFile) and \
                    os.access(self.dirName + os.sep + self.nzbFile, os.R_OK):
                move(self.dirName + os.sep + self.nzbFile,
                     self.dirName + os.sep + Hellanzb.PROCESSED_SUBDIR + os.sep + self.nzbFile)

        # Move out anything else that's broken, a dupe or tagged as
        # not required
        for file in self.brokenFiles:
            if os.path.isfile(self.dirName + os.sep + file):
                move(self.dirName + os.sep + file,
                     self.dirName + os.sep + Hellanzb.PROCESSED_SUBDIR + os.sep + file)

        for file in os.listdir(self.dirName):
            ext = getFileExtension(file)
            if ext != None and len(ext) > 0 and ext.lower() not in Hellanzb.KEEP_FILE_TYPES and \
                   ext.lower() in Hellanzb.NOT_REQUIRED_FILE_TYPES:
                move(self.dirName + os.sep + file,
                     self.dirName + os.sep + Hellanzb.PROCESSED_SUBDIR + os.sep + file)
                
            elif re.match(r'.*_duplicate\d{0,4}', file):
                move(self.dirName + os.sep + file,
                     self.dirName + os.sep + Hellanzb.PROCESSED_SUBDIR + os.sep + file)

        handledPars = False
        if os.path.isfile(self.dirName + os.sep + Hellanzb.PROCESSED_SUBDIR + \
                          os.sep + '.par_done'):
            handledPars = True
        
        # Finally, nuke the processed dir. Hopefully the PostProcessor did its job and
        # there was absolutely no need for it, otherwise tough! (and disable the option
        # and try again) =]
        if hasattr(Hellanzb, 'DELETE_PROCESSED') and Hellanzb.DELETE_PROCESSED:
            msg = 'Deleting processed dir: ' + self.dirName + os.sep + \
                Hellanzb.PROCESSED_SUBDIR + \
                ', it contains: ' + str(walk(self.dirName + os.sep + \
                                             Hellanzb.PROCESSED_SUBDIR,
                                             1, return_folders = 1))
            logFile(msg)
            rmtree(self.dirName + os.sep + Hellanzb.PROCESSED_SUBDIR)

        # Finished. Move dirName to DEST_DIR if we need to
        self.moveDestDir()
        
        # We're done
        e = time.time() - self.startTime 
        if not self.isSubDir:
            parMessage = ''
            if not handledPars:
                parMessage = ' (No Pars)'
                
            info((archiveName(self.dirName) + ': Finished processing (took: %.1fs)' + \
                 parMessage) % (e))

            if parMessage != '':
                parMessage = '\n' + parMessage
            growlNotify('Archive Success', 'hellanzb Done Processing' + parMessage + ':',
                        archiveName(self.dirName), True)
                       #self.background)
        # FIXME: could unsticky the message if we're running hellanzb.py -p
        # and preferably if the post processing took say over 30 seconds

    def postProcess(self):
        """ process the specified directory """
        # Check for shutting down flag before doing any significant work
        self.startTime = time.time()
        checkShutdown()
        
        # Put files we've processed and no longer need (like pars rars) in this dir
        processedDir = self.dirName + os.sep + Hellanzb.PROCESSED_SUBDIR

        if not os.path.exists(self.dirName):
            raise FatalError('Directory does not exist: ' + self.dirName)
        elif not os.path.isdir(self.dirName):
            raise FatalError('Not a directory: ' + self.dirName)
                              
        if not os.path.exists(processedDir):
            try:
                os.mkdir(processedDir)
            except OSError, ose:
                # We might have just unrared something with goofy permissions.
                
                # FIXME: hope we don't need the processed dir! this would typically only
                # happen for say a VIDEO_TS dir anyway
                warn('Unable to create processedDir: ' + processedDir + ' err: ' + str(ose))
                pass

                # FIXME: If we just unrared a directory bad perms, ideally we should fix
                # the perms
                #if ose.errno == errno.EACCES:
                #    os.chmod(processedDir, 

        elif not os.path.isdir(processedDir):
            raise FatalError('Unable to create processed dir, a non dir already exists there: ' + \
                             processedDir)
    
        # First, find broken files, in prep for repair. Grab the msg id and nzb
        # file names while we're at it
        self.brokenFiles = []
        files = os.listdir(self.dirName)
        for file in files:
            absoluteFile = self.dirName + os.sep + file
            if os.path.isfile(absoluteFile):
                if stringEndsWith(file, '_broken'):
                    # Keep track of the broken files
                    self.brokenFiles.append(absoluteFile)
                    
                elif len(file) > 7 and file[0:len('.msgid_')] == '.msgid_':
                    self.msgId = file[len('.msgid_'):]
    
                elif len(file) > 3 and file.find('.') > -1 and getFileExtension(file).lower() == 'nzb':
                    self.nzbFile = file
    
        # If there are required broken files and we lack pars, punt
        if len(self.brokenFiles) > 0 and containsRequiredFiles(self.brokenFiles) and \
                not dirHasPars(self.dirName):
            errorMessage = 'Unable to process directory: ' + self.dirName + '\n' + \
                'This directory has the following broken files: '
            for brokenFile in self.brokenFiles:
                errorMessage += '\n' + ' '*4 + brokenFile
            errorMessage += '\nand contains no par2 files for repair'
            raise FatalError(errorMessage)
        
        if dirHasPars(self.dirName):
            checkShutdown()
            processPars(self.dirName)
        
        if dirHasRars(self.dirName):
            checkShutdown()
            processRars(self.dirName, self.rarPassword)

        if dirHasMusic(self.dirName):
            checkShutdown()
            self.processMusic()

        # Assemble split up files
        #assembleSplitFiles(self.dirName)

        # FIXME: do we need to gc.collect() after post processing a lot of data?

        # Post process sub directories
        trolled = 0
        for file in os.listdir(self.dirName):
            if file == Hellanzb.PROCESSED_SUBDIR:
                continue
            
            if os.path.isdir(pathjoin(self.dirName, file)):
                if not self.isSubDir:
                    troll = PostProcessor(pathjoin(self.dirName, file),
                                          parentDir = self.dirName)
                else:
                    troll = PostProcessor(pathjoin(self.dirName, file),
                                          parentDir = self.parentDir)
                troll.run()
                trolled += 1
                
        self.finishedPostProcess()

"""
/*
 * Copyright (c) 2005 Philip Jenvey <pjenvey@groovie.org>
 *                    Ben Bangert <bbangert@groovie.org>
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
 * $Id$
 */
"""
