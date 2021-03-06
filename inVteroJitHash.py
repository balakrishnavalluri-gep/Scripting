"""
##############################################################################################
    
    Copyright(C) 2017 Shane Macaulay smacaulay@gmail.com

    This program is free software: you can redistribute it and/or modify
    it under the terms of the GNU Affero General Public License as
    published by the Free Software Foundation, either version 3 of the
    License, or(at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.See the
    GNU Affero General Public License for more details.

    You should have received a copy of the GNU Affero General Public License
    along with this program.If not, see<https://www.gnu.org/licenses/>.

##############################################################################################

V.3 CHANGELOG

 * Finally Fast Forensics edition(tm)!!
   * Improved client side SSL cert checking of server (secure++)
   * Faster than doing a dlldump to you're local disk
   * Better than doing a dlldump since you get something usefull
   * Smarter than doign so since you should have 99/100 of the junk you don't care about out of you're way

 * Added heaps of colors!

 * Kernel drivers

 * Removed some of the slower servers in the back-end to allow for high speed validations (no slow!)
    + Tuned retries and async socket I/O with gevents

 * Improved error responses and overall feedback to user

 * Metrics and information regarding verification and a few new command line parameters for extra details
   + Block offset that has the modified code
   + Special dump mode for just modified code

 * Needful dependencies in the txt file

To use this with volatility place this .py anywhere, ensure you have volatility working.
For example the command line below will simply run the invterojithash against the input memory image

*********************************************************************************************************
python vol.py --plugins=[path-to-folder-where-this-code-is] -f "/path/to/temp/10 ENT 1607-Snapshot1.vmem"
 --profile=Win10x64_14393 invterojithash -x
*********************************************************************************************************

I'll be looking to make updates feel free to give me some issues through "github.com/K2"

OPERATIONS: The client script you run perform's a basic sha256 of whatever is in memory with no regard
for relocations or anything.  Very simple.  All of the heavy lifting magic is done on the server time
on demand integrity hashes are computed based on you're client's described virtual address.
i.e. You say kernel32 is loaded at address X.  The server responds and adjusts it's hash database in real time
so there is very little work on the client side.

I haven't written the PE header fixes yet for this code as it's currently done for the PowerShell, in effect
there are so many changes for the PE header, it's like a shotgun blast of bits that need adjusting.

You can setup you're own JIT hash server and host local to perform integrity checks.

// TODO: Add kernel modules/space

// TODO: What about if you submit the modified pages to the server I'll report back a diff view?

Enjoy!
################################################################################################
"""
from gevent import monkey
monkey.patch_all()

import urllib3.contrib.pyopenssl
urllib3.contrib.pyopenssl.inject_into_urllib3()

import volatility.addrspace
import volatility.commands as commands
import volatility.utils as utils
import volatility.win32.tasks as tasks
import volatility.win32.modules as modules
import os, time, base64, sys, threading
import json, urllib, urllib2, urllib3
import gevent, struct, retry, traceback, colorama
import ntpath, certifi

from os import environ
from retry import retry
from gevent import monkey
from struct import unpack
from gevent.pool import Pool
from gevent.queue import Queue
from Crypto.Hash import SHA256
from traceback import print_tb, print_exc
from urllib2 import HTTPError, URLError
from gevent.event import AsyncResult
from colorama import init, AnsiToWin32
from colored import fg, bg, attr
from tqdm import tqdm
from multiprocessing import freeze_support, RLock

class inVteroJitHash(commands.Command):
    '''
    Use the public free inVtero JIT Page hash server to respond with integrity information.

    The JitPageHash service endpoint is running with the json2pdb job.]
    "https://pdb2json.azurewebsites.net/api/PageHash/x"

    Below is a sample "python.requests" request/response that demonstrates the expected functionality.
    The response information is very terse so it's a good idea to maintain some meta-information
    across the request since it's pumped into the data render_text method.

    ---- snip -- snip ---- ( below is copy/pasteable into a python shell to test ) ---- snip -- snip ----
    import requests
    req_json = {
        "HdrHash":  "QUTB1TPisyVGMq0do/CGeQb5EKwYHt/vvrMHcKNIUR8=",
        "TimeDateStamp":  3474455660,
        "AllocationBase":  140731484733440,
        "BaseAddress":  140731484737536,
        "ImageSize":  1331200,
        "ModuleName":  "ole32.dll",
        "HashSet":[
            {
                "Address":  140731484798976,
                "Hash":  "+REyeLCxvwPgNJphE6ubeQVhdg4REDAkebQccTRLYL8="
            },
            {
                "Address":  140731484803072,
                "Hash":  "xQJiKrNHRW739lDgjA+/1VN1P3VSRM5Ag6OHPFG6594="
            },
            {
                "Address":  140731484807168,
                "Hash":  "ry9yVHhDQohYTfte0A4iTmNY8gDDfKUmFpxsWF67rtA="
            },
            {
                "Address":  140731484811264,
                "Hash":  "bk31Su+2qFGhZ8PLN+fMLDy2SqPDMElmj0EZA62LX1c="
            },
            {
                "Address":  140731484815360,
                "Hash":  "0RyIKfVFnxkhDSpxgzPYx2azGg59ht4TbVr66IXhVp4="
            }
        ]
    }
    requests.post("https://pdb2json.azurewebsites.net/api/PageHash/x", json=req_json).json()

    ---- snip -- snip ---- the lines below are the output of the above service call ---- snip -- snip  ----

    [{u'Address': 140731484733440L, u'HashCheckEquivalant': True},
    {u'Address': 140731484798976L, u'HashCheckEquivalant': True},
    {u'Address': 140731484803072L, u'HashCheckEquivalant': True},
    {u'Address': 140731484807168L, u'HashCheckEquivalant': True},
    {u'Address': 140731484811264L, u'HashCheckEquivalant': True},
    {u'Address': 140731484815360L, u'HashCheckEquivalant': True}]
    '''
    #JITHashServer = "http://localhost:7071/api/PageHash/x"
    JITHashServer = "https://pdb2json.azurewebsites.net/api/PageHash/x"

    # Tune this if you want to hit the server harder
    pool = Pool()
    greenlits = []

    total_miss = {}
    null_hash = None
    stream = None
    VBValidated = 0
    ScannedMods = 0
    VirtualBlocksChecked = 0
    StartTime = time.time()
    MissList = []
    TotalProgress = []
    TotBytesValidated = 0
    TotalLastN = 0
    TotPercent = 0.0
    TotalBar = None
    logg = None
    DumpFolder = None
    headers = {'Content-Type':'application/json', 'Accept':'text/plain'}
    http = urllib3.PoolManager(maxsize=512, block=True, headers = headers, cert_reqs='CERT_REQUIRED', ca_certs=certifi.where())
    def __init__(self, config, *args):
        # no color on Windows yet, this keeps the output from looking insane with all the ANSI
        if os.name == 'nt':
            self.stream = AnsiToWin32(sys.stdout, convert=True).stream
            init()
            #init(convert=True)
        commands.Command.__init__(self, config, *args)
        config.add_option('SuperVerbose', short_option='s', help='Display per page validation results.', action='store_true', default=False)
        config.add_option('ExtraTotals', short_option='x', help='List of all misses per-module.', action='store_true', default=False)
        config.add_option('DumpFolder', short_option='D', help='Dump the failed blocks to a specified folder', default=None)
        config.add_option('FailFile', short_option='F', help='Output file containing detailed information about unverifiable memory', default='FailedValidation.txt')
        if os.name is not 'nt':
            os.system('setterm -cursor off')
        else:
            os.system('color 0f')

    # This method is a huge bit of code that should of been in volatility
    # Anyhow, NX bit's need to be checked at every layer of the page table.
    # NX also _IS_ supported on IA32 PAE here... it's a real thing.
    @classmethod
    def is_nxd(cls, vaddr, addr_space):
        """
        Is the page for a given virtualaddress to be restricted from execution or not present?

        The return value True is something we are ignoring. False means it's present and unrestricted.

        Parameters
        ----------
        vaddr : long
            A virtual address from IA32PAE or AMD64 compatible address spaces 
        addr_space : Addrspace
            An instance of the address space that contains our page table

        Returns
        -------
        Boolean
            True means that the page at address vaddr is ignored based on NX or missing by means of not having the "valid" bit set in the page table
        """
        vaddr = long(vaddr)
        if isinstance(addr_space, volatility.plugins.addrspaces.amd64.AMD64PagedMemory) is False:
            pdpe = addr_space.get_pdpi(vaddr)
            if not addr_space.entry_present(pdpe):
                return True
            pgd = addr_space.get_pgd(vaddr, pdpe)
            if not addr_space.entry_present(pgd):
                return True
            if addr_space.page_size_flag(pgd):
                return cls.is_nx(pgd)
            else:
                pte = addr_space.get_pte(vaddr, pgd)
                if not addr_space.entry_present(pte):
                    return True
                return cls.is_nx(pte)
        else:
            pml4e = addr_space.get_pml4e(vaddr)
            if not addr_space.entry_present(pml4e):
                return True
            pdpe = addr_space.get_pdpi(vaddr, pml4e)
            if not addr_space.entry_present(pdpe):
                return True
            if addr_space.page_size_flag(pdpe):
                return cls.is_nx(pdpe)
            pgd = addr_space.get_pgd(vaddr, pdpe)
            if addr_space.entry_present(pgd):
                if addr_space.page_size_flag(pgd):
                    return cls.is_nx(pgd)
                else:
                    pte = addr_space.get_pte(vaddr, pgd)
                    if not addr_space.entry_present(pte):
                        return True
                    return cls.is_nx(pte)
            return True
        raise ValueError('The underlying address space does not appear to be supported', type(addr_space), addr_space)
    
    @staticmethod
    def is_nx(entry):
        """
        Return if the most significant bit is set.

        The most significant bit represents the "NO EXECUTE" or "EXECUTION DISABLED" flag for IA32PAE and AMD64 ABI's

        Parameters
        ----------
        entry : long
            An entry from the page table.
        
        Returns
        -------
            The status of the NX/XD bit.
        """
        return entry & (1 << 63) == (1 << 63)

    def mod_get_ptes(self, mod, addr_space):
        for vpage in range(mod.DllBase, mod.DllBase + mod.SizeOfImage, 4096):
            yield vpage, self.is_nxd(vpage, addr_space)

    # return a sha256 from the input bytes, the server is only configured with SHA256
    # since it's JIT hash in it's core, we can upgrade this at any time... Future Proof !
    def HashPage(self, data):
        if data is None:
            return "NULL INPUT"
        try:
            memoryview(data)
        except TypeError:
            return "NULL INPUT"
        sha = SHA256.new()
        sha.update(data)
        hashB64 = base64.b64encode(sha.digest())
        if hashB64 is self.null_hash:
            return "NULL INPUT"
        return hashB64

    # if this is lagging you out dial back the tries/delay... i'm pretty aggressive here
    @retry(HTTPError, tries=16, delay=3, backoff=2)
    def pozt(self, LocalMod):
        rvData = ""
        try:
            data = LocalMod["json"]
            dataEncoded = json.dumps(data)
            #req = self.http.request('POST', self.JITHashServer, body=dataEncoded)
            req = self.http.urlopen('POST', self.JITHashServer, headers=self.headers, body=dataEncoded)
            #response = self.http.urlopen(req)
            #rvData = req.data
        except HTTPError as inst:
            if inst.code == 204:
                return rvData
        except:
            print("{}{}".format(fg("red"), "SERVER FAILED DESPITE MULTIPLE ATTEMPTS"))
            print("{}{}{}[{}]".format(fg("navajo_white_1"), "Exception ", fg("light_magenta"), str(sys.exc_info()[0])))
            for x in sys.exc_info():
                print("{}{}".format(fg("hot_pink_1b"), x))
        finally:
            a = AsyncResult()
            a.set(req.data)
            LocalMod["resp"] = a.get(block=True)
            req.release_conn()
            self.output(LocalMod)

        return LocalMod

    # Volatility's contract defines this as the entry point for modules.  Here we do all of our work and orchastrate our internal async/coroutines through
    # the entire execution.  The completion routine render_text is for a minimal amount of reporting.

    def calculate(self):
        self.DumpFolder = (self._config.DumpFolder or None)
        self.logg = open(self._config.FailFile, mode="w+", buffering=8192)
        self.logg.write("On Windows, use \"type [Filename]\" for best results (Win10) {} JIT hash log file\n".format(fg("cornflower_blue")))
        # get the null hash (at runtime in case a different hash is used etc..)
        null_page = bytearray(4096)
        self.null_hash = self.HashPage(null_page)

        addr_space = utils.load_as(self._config)
        if isinstance(addr_space, volatility.plugins.addrspaces.intel.IA32PagedMemory) and not isinstance(addr_space, volatility.plugins.addrspaces.intel.IA32PagedMemoryPae):
            raise "The memory model of this memory dump dates from the 1990's and does not support execute protection."

        outputJobs = None
        taski = 0
        taskCnt = 0
        tasklist = tasks.pslist(addr_space)
        for _ in tasks.pslist(addr_space):
            taskCnt += 1

        print("{}{}{} [{}]{}".format(fg("chartreuse_1"), "pdb2json JIT PageHash calls under way...  endpoint ", fg("hot_pink_1b"), self.JITHashServer, fg("sky_blue_1"), attrs=["bold"]))
        bformat = "{elapsed}{l_bar}{postfix}{bar}"
        self.TotalBar = tqdm(desc="{}TotalProgress".format(fg("cornflower_blue"), total=taskCnt, position=0, mininterval=0.5, bar_format=bformat))
        # The timer is reset here since were not counting the coldstartup time
        self.StartTime = time.time()
        for task in tasklist:
            taski += 1

            proc_as = task.get_process_address_space()
            mods = []
            # Volatility workaround as there is not a consistant interface I know of
            # to handle AS the same way for kernel & user
            if task.UniqueProcessId == 4:
                mods = list(modules.lsmod(addr_space))
                proc_as = addr_space
            else:
                mods = list(task.get_load_modules())

            TaskName = "[" + task.ImageFileName + "-" + str(task.UniqueProcessId) + "]"

            taskBar = tqdm(desc=TaskName, total=len(mods), position=1, leave=False, mininterval=0.5, bar_format=bformat)
            p = dict({"Name":TaskName, "Task":task, "TaskBlockCount":0, "ModContext":[], "bar":taskBar})
            for mod in mods:
#@                taskBar.set_postfix_str('{} modules'.format(len(mods), refresh=True)
                hashAddr = []
                hashVal = []
                for vpage, nx in self.mod_get_ptes(mod, proc_as):
                    if(nx):
                        continue
                    data = proc_as.read(vpage, 4096)
                    if data is None or data is self.null_hash:
                        continue
                    hashAddr.append(str(vpage))
                    hashVal.append(self.HashPage(data))
            # these statements are yet another workaround for volatility
            # for some unknown reason these data structures have never been written into Volatility...
            # of course you can acquire the timestamp by reading the nt_header/fileheader/etc but that data is
            # significantly lower quality given that it can be modified at any time.  The kernel data structure
            # remains valid unless the attacker kills the process etc... In any event (hah) since this value has never changed
            # I hard coded it here for simplicity.  Perhaps I should enforce always using it, will circle back 360 on that.. :O

                timevalue = mod.TimeDateStamp
                #this should only work for kernel space modules
                if timevalue == 0 and task.UniqueProcessId == 4:
                    timeLoc = self.to_int64(mod.v() + 0x9c)
                    redInBytes = addr_space.read(timeLoc, 4)
                    if redInBytes is not None and len(redInBytes) == 4:
                        timevalue = unpack("<L", redInBytes)[0]
                req_hdr = {
                    "ModuleName": str(mod.FullDllName or ''),
                    "ImageSize": str(mod.SizeOfImage),
                    "BaseAddress": str(mod.DllBase),
                    "AllocationBase": str(mod.DllBase),
                    "TimeDateStamp": str(int(timevalue)),
                    "HdrHash": self.HashPage(proc_as.read(mod.DllBase, 4096)),
                    "HashSet": [{"Address": a, "Hash": h} for a, h in zip(hashAddr, hashVal)]
                }
                if req_hdr["ModuleName"] is '':
                    self.logg.write("{}{}{}: Unable to scan anonymous executable memory. {:#x} length: {:#x}{}.\n".format(bg("black"), fg("yellow_2"), TaskName, mod.DllBase, mod.SizeOfImage, fg("cornflower_blue")))
                    filename = "{}/{}-{:#x}".format(self.DumpFolder, TaskName, mod.DllBase)
                    open(filename, 'w').close()
                    for vpage in range(mod.DllBase, mod.DllBase + mod.SizeOfImage, 4096):
                        data = proc_as.read(vpage, 4096)
                        if self.DumpFolder is not None and data is not None:
                            with open(filename, 'ab') as block:
                                block.write(bytearray(data))
                else:
                    LocalMod = dict({"Module":mod, "Ctx":p, "ModBlockCount":hashAddr.count, "json":req_hdr, "AS":addr_space})
                    p["TaskBlockCount"] = p["TaskBlockCount"] + len(hashAddr)
                    taskBar.update(1)
                    self.pool.spawn(self.pozt, LocalMod)

             #= [gevent.spawn(self.pozt, cx) for cx in p["ModContext"]]
            #gevent.wait(outputJobs)
            self.TotalBar.update(1)

    # Ulong64 would be nice, this is a needed workaround
    @staticmethod
    def to_int64(n):
        """Kludge for 64bit unsigned type"""
        n = n & ((1 << 64) - 1)
        if n > (1 << 63) - 1:
            n -= 1 << 64
        return n

    @staticmethod
    def PercentToColor(Validated):
        level = fg("sky_blue_1")
        if Validated < 100.0:
            level = fg("cornflower_blue")
        if Validated < 80.0:
            level = fg("light_sky_blue_3a")
        if Validated < 60.0:
            level = fg("yellow_2")
        if Validated < 40.0:
            level = fg("purple_1a")
        if Validated < 20.0:
            level = fg("deep_pink_4c")
        if Validated < 5.0:
            level = fg("red_1")
        return level

    # this method is really just a bunch of console I/O reporting on the service calls
    def output(self, Local):
        """Output data in a nonstandard but fun and more appealing way."""
        bar = Local["Ctx"]["bar"]
        try:
            addr_space = Local["AS"]
            task = Local["Ctx"]["Task"]
            req_hdr = Local["json"]
            r = Local["resp"]            
            rj = None

            moduleName = ""
            if req_hdr.has_key("ModuleName"):
                moduleName = req_hdr["ModuleName"]

            info = "{}[{:<}]".format(fg("spring_green_2b"), ntpath.basename(moduleName))

            self.ScannedMods += 1
            ModBlksValidated = 0
            modMissedBlocks = []

            if r is not None:
                if len(r) < 1:
                    return
                rj = json.loads(r)
                modPageCount = r.count("{") - 1
                if modPageCount == 0:
                    modPageCount = 1
                self.VirtualBlocksChecked += modPageCount

                # parse the response in a structured way
                if rj is not None:
                    for rarr in rj:
                        if rarr["HashCheckEquivalant"] is True:
                            ModBlksValidated += 1
                            self.VBValidated += 1
                        else:
                            modMissedBlocks.append(long(rarr["Address"]))

                if self._config.ExtraTotals is True:
                    if not self.total_miss.has_key(moduleName):
                        self.total_miss[moduleName] = (modPageCount, ModBlksValidated)
                    else:
                        currCnt = self.total_miss[moduleName]
                        self.total_miss[moduleName] = (currCnt[0] + modPageCount, currCnt[1] + ModBlksValidated)

                validPct = float((ModBlksValidated * 100.0) / modPageCount)
                level = self.PercentToColor(validPct)
                if modPageCount == 1:
                    if ModBlksValidated == 1:
                        level = fg("grey_19")
                    if ModBlksValidated == 0:
                        level = fg("grey_35")

                infoLine="{:<}{:>6x}/{}{:<6x}{:<}[{:<2.2f}%]{}{}".format(fg("light_steel_blue_1"), ModBlksValidated<<12, fg("white"), modPageCount<<12, level, validPct, fg("light_green"), info)
                
                if validPct < 100.0:
                    TaskName = Local["Ctx"]["Name"]
                    self.logg.writelines(("Failures detected: ", infoLine,"\t: ", TaskName, "\r\n", "BlockAddrs:   "))
                    
                    #if self._config.SuperVerbose is True:
                    for mb in modMissedBlocks:
                        # by default skip headers
                        if mb != req_hdr["BaseAddress"]:
                            self.logg.write("{:#14x} ".format(mb))
                            if self.DumpFolder is not None:
                                proc_as = task.get_process_address_space()
                                if task.UniqueProcessId == 4:
                                    proc_as = addr_space
                                    
                                data = proc_as.read(mb, 4096)
                                if data is not None:
                                    with open("{}/{}-{:#x}".format(self.DumpFolder, TaskName, mb), 'wb') as block:
                                        block.write(bytearray(data))
                    self.logg.write('\n')
                
                bar.set_postfix_str('{:<}'.format(infoLine))
                bar.update(1)            
        except:
            print_exc()
        #update less frequently put this back in
        #if self.TotalBar.n > self.TotalLastN:
        self.TotBytesValidated = self.VBValidated << 12
        self.TotalBytesChecked = self.VirtualBlocksChecked << 12
        self.TotPercent = (self.VBValidated * 100.0 / self.VirtualBlocksChecked)
        self.TotalLastN = self.TotalBar.n
        self.TotalBar.set_postfix_str("{:<}[{:<2.3f}%]{:}[{:,}]{}{}[{:,}]{}".format(self.PercentToColor(self.TotPercent), self.TotPercent, fg("white"), self.TotBytesValidated, fg("sky_blue_1"), "/", self.TotalBytesChecked, fg("light_green")))

    def render_text(self, outfd, data):
        if os.name is not 'nt':
            os.system('setterm -cursor on')
        
        print "{}{}".format(fg("hot_pink_1b"), "Join in progress of any outstanding async operations.")
        gevent.joinall(self.pool)

        if self.VirtualBlocksChecked == 0:
            print ("{}{}".format(fg("yellow_2"), "error, nothing was processed"))
        else:
            RuntimeSeconds = int(time.time() - self.StartTime)
            print ("\r\n\r\n{}{}{}[{}]{}{}".format(fg("sky_blue_1"), "Run Time ", fg("light_green"), str(RuntimeSeconds), fg("sky_blue_1"), " seconds."))
            self.TotBytesValidated = self.VBValidated << 12
            self.TotalBytesChecked = self.VirtualBlocksChecked << 12
            self.TotPercent = (self.VBValidated * 100.0 / self.VirtualBlocksChecked)
            print ("{}{}{}[{:,}]{}{}".format(fg("sky_blue_1"), "A total of ", fg("light_green"), self.ScannedMods, fg("sky_blue_1"), " modules scanned."))
            print ("{}{}[{:,}]{}{}{}[{:,}]".format("Scanned Pages: ", fg("light_green"), self.VirtualBlocksChecked, fg("sky_blue_1"), ". Pages valid: ", fg("light_green"), self.VBValidated)),
            print (" {}[{:2.3f}%]{}{}{}[{:,}]{}{}{}[{:,}]".format(self.PercentToColor(self.TotPercent), self.TotPercent, fg("sky_blue_1"), " assurance. Validated bytes: ", fg("light_green"),self.TotBytesValidated, fg("sky_blue_1"), "/", fg("light_green"), self.TotalBytesChecked))
            print ("{}{} {:,} {}".format(fg("white"), "Total I/O throughput:", self.TotalBytesChecked / RuntimeSeconds, "bytes per second."))
        
        for key in self.total_miss:
            miss_info = "{}{} - {}".format(fg("hot_pink_1b"), key, self.total_miss[key])
            self.logg.writelines((miss_info, "\n"))
            if self._config.ExtraTotals is True:
                print (miss_info)
        if os.name is 'nt':
            os.system('color')