# Volatility
# Copyright (C) 2007-2011 Volatile Systems
#
# Additional Authors:
# Michael Cohen <scudette@users.sourceforge.net>
# Mike Auty <mike.auty@gmail.com>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or (at
# your option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA 02111-1307 USA
#

import logging
import os

from volatility.plugins.windows import common
from volatility import plugin


class WinPsList(common.WinProcessFilter):
    """List processes for windows."""

    __name = "pslist"

    kdbg = None
    eprocess = None

    def __init__(self, eprocess=None, **kwargs):
        """Lists the processes by following the _EPROCESS.PsActiveList.

        In the windows operating system, processes are linked together through a
        doubly linked list. This plugin follows the list around, printing
        information about each process.

        To begin, we need to find any element on the list. This can be done by:

        1) Obtaining the _KDDEBUGGER_DATA64.PsActiveProcessHead - debug
           information.

        2) Finding any _EPROCESS in memory (e.g. through psscan) and following
           its list.

        This plugin supports both approaches.

        Args:
          eprocess: The location of any eprocess location (in kernel AS). This
             can be obtained from e.g. psscan or find_dtb. If neither kdbg or
             eprocess are specified we just do the best we have from the
             session.
        """
        super(WinPsList, self).__init__(**kwargs)
        self.eprocess = eprocess

    def list_eprocess_from_kdbg(self, kdbg):
        """List the eprocess using the kdbg method."""
        PsActiveList = kdbg.PsActiveProcessHead.dereference_as(
            "_LIST_ENTRY")

        return iter(PsActiveList.list_of_type(
                "_EPROCESS", "ActiveProcessLinks"))

    def list_eprocess_from_eprocess(self, eprocess_offset):
        eprocess = self.profile.Object(
            theType="_EPROCESS",
            offset=eprocess_offset, vm=self.kernel_address_space)

        for task in eprocess.ActiveProcessLinks:
            # TODO: Need to filter out the PsActiveProcessHead (which is not
            # really an _EPROCESS)
            yield task

    def list_eprocess(self):
        if self.kdbg:
            return self.list_eprocess_from_kdbg(self.kdbg)
        elif self.eprocess:
            return self.list_eprocess_from_eprocess(self.eprocess)

        logging.debug("Unable to list processes using any method.")
        return []

    def render(self, renderer):
        renderer.table_header( [("Offset (V)", "[addrpad]"),
                                ("Name", "20s"),
                                ("PID", ">6"),
                                ("PPID", ">6"),
                                ("Thds", ">6"),
                                ("Hnds", ">8"),
                                ("Sess", ">6"),
                                ("Wow64", ">6"),
                                ("Start", "20"),
                                ("Exit", "20")]
                               )

        for task in self.filter_processes():
            renderer.table_row(task.obj_offset,
                               task.ImageFileName,
                               task.UniqueProcessId,
                               task.InheritedFromUniqueProcessId,
                               task.ActiveThreads,
                               task.ObjectTable.HandleCount,
                               task.SessionId,
                               task.IsWow64,
                               str(task.CreateTime or ''),
                               str(task.ExitTime or ''),
                               )


class WinDllList(common.WinProcessFilter):
    """Prints a list of dll modules mapped into each process."""

    __name = "dlllist"


    def render(self, renderer):
        for task in self.filter_processes():
            pid = task.UniqueProcessId

            renderer.write(u"*" * 72 + "\n")
            renderer.write(u"{0} pid: {1:6}\n".format(task.ImageFileName, pid))

            if task.Peb:
                renderer.write(u"Command line : {0}\n".format(
                        task.Peb.ProcessParameters.CommandLine))

                if task.IsWow64:
                    renderer.write(
                        u"Note: use ldrmodules for listing DLLs in Wow64 processes\n")

                renderer.write(u"{0}\n".format(task.Peb.CSDVersion))
                renderer.write(u"\n")
                renderer.table_header([("Base", "[addrpad]"),
                                       ("Size", "[addr]"),
                                       ("Path", ""),
                                       ])
                for m in task.get_load_modules():
                    renderer.table_row(m.DllBase, m.SizeOfImage, m.FullDllName)
            else:
                renderer.write("Unable to read PEB for task.\n")


class WinMemMap(common.WinProcessFilter):
    """Calculates the memory regions mapped by a process."""

    __name = "memmap"

    def get_pages_for_eprocess(self, eprocess, coalesce=False):
        """Returns the list of pages the _EPROCESS has mapped.

        Args:
          eprocess: A _EPROCESS to use.
          coalesce: Should memory ranges be coalesced into larger blocks.

        Yields:
          Tuples of (virtual address, physical address, range length)
        """
        last_va = 0
        last_pa = 0
        last_len = 0

        task_address_space = eprocess.get_process_address_space()
        for va, length in task_address_space.get_available_addresses():
            pa = task_address_space.vtop(va)
            if pa == None:
                continue

            ## This page is right after the last page in the range
            if coalesce and va == last_va + last_len and pa == last_pa + last_len:
                last_len += length
            else:
                if last_len > 0:
                    yield (last_va, last_pa, last_len)

                last_va, last_pa, last_len = va, pa, length

        yield (last_va, last_pa, last_len)

    def render(self, renderer):
        for task in self.filter_processes():
            renderer.write("*" * 72 + "\n")
            task_space = task.get_process_address_space()
            renderer.write(u"Process: '{0}' pid: {1:6}\n".format(
                    task.ImageFileName, task.UniqueProcessId))

            ranges = list(self.get_pages_for_eprocess(task))
            if not ranges:
                renderer.write("Unable to read pages for task.\n")
                continue

            renderer.table_header([("Virtual", "[addrpad]"),
                                   ("Physical", "[addrpad]"),
                                   ("Size", "[addr]")])

            for virtual_address, phys_address, length in ranges:
                renderer.table_row(virtual_address, phys_address, length)


class WinMemDump(WinMemMap):
    """Dump the addressable memory for a process"""

    __name = "memdump"

    def __init__(self, dump_dir=None, **args):
        """Dump all addressable memory for a process.

        Note: This can be quite large. No padding is inserted into the output
        file to compensate for inaddressable process memory, hence the output
        offsets are essentially random. It is almost always better to use the
        fuse filesystem in tools/windows/address_space_fuse.py

        Args:
          dump_dir: The Directory in which to dump memory. Files of the form
            pid.dmp will be created there.
        """
        super(WinMemDump, self).__init__(**args)
        self.dump_dir = dump_dir

    def dump_process(self, eprocess, fd):
        for va, pa, length in self.get_pages_for_eprocess(eprocess):
            fd.write(self.physical_address_space.read(pa, length))

    def render(self, outfd):
        if self.dump_dir is None:
            raise plugin.PluginError("Dump directory not specified.")

        for task in self.filter_processes():
            outfd.write("*" * 72 + "\n")
            filename = u"{0}_{1:d}.dmp".format(task.ImageFileName, task.UniqueProcessId)

            outfd.write(u"Writing {0} {1:6} to {2}\n".format(
                    task.ImageFileName, task, filename))

            with open(os.path.join(self.dump_dir, filename), 'wb') as fd:
                self.dump_process(task, fd)