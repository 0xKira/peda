#       PEDA - Python Exploit Development Assistance for GDB
#
#       Copyright (C) 2012 Long Le Dinh <longld at vnsecurity.net>
#
#       License: see LICENSE file for details
#

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import re
import csv
import os
import sys
import shlex
import string
import time
import traceback
import codecs
import gdb

# point to absolute path of peda.py
PEDAFILE = os.path.abspath(os.path.expanduser(__file__))
if os.path.islink(PEDAFILE):
    PEDAFILE = os.readlink(PEDAFILE)
sys.path.insert(0, os.path.dirname(PEDAFILE) + "/lib/")

# Use six library to provide Python 2/3 compatibility
import six
from six.moves import range, input
try:
    import six.moves.cPickle as pickle
except ImportError:
    import pickle

from shellcode import SHELLCODES, Shellcode
import utils
from utils import normalize_argv, memoized, format_reference_chain, format_disasm_code
from utils import to_int, to_hex, to_hexstr, hex2str, to_address, int2hexstr, list2hexstr, str2intlist
from utils import u32, u64, p32, p64
from utils import msg, warning_msg, error_msg, separator, pager
from utils import green, red, yellow, blue, purple, cyan
import config
from nasm import Nasm

if sys.version_info.major == 3:
    pyversion = 3
else:
    pyversion = 2


REGISTERS = {
    8: ["al", "ah", "bl", "bh", "cl", "ch", "dl", "dh"],
    16: ["ax", "bx", "cx", "dx"],
    "elf32-i386": ["eax", "ebx", "ecx", "edx", "esi", "edi", "ebp", "esp", "eip"],
    "elf64-x86-64": [
        "rax", "rbx", "rcx", "rdx", "rsi", "rdi", "rbp", "rsp", "rip", "r8", "r9", "r10", "r11", "r12", "r13", "r14",
        "r15"
    ],
    "elf32-littlearm":
    ["r0", "r1", "r2", "r3", "r4", "r5", "r6", "r7", "r8", "r9", "r10", "r11", "r12", "sp", "lr", "pc"],
    "elf32-tradlittlemips": [
        "a0", "a1", "a2", "a3", "t0", "t1", "t2", "t3", "t4", "t5", "t6", "t7", "t8", "t9", "s0", "s1", "s2", "s3",
        "s4", "s6", "s6", "s7", "gp", "sp", "s8", "ra", "pc"
    ],
    "elf32-powerpc":
    list(map(lambda x: "r%i" % x, range(32))) + ["pc", "lr"],
    "elf64-littleaarch64":
    list(map(lambda x: "x%i" % x, range(31))) + ["sp", "pc"] + list(map(lambda x: "w%i" % x, range(31)))
}

# pwndbg/commands/__init__.py
_mask = 0xffffffffFFFFFFFF
_mask_val_type = gdb.Value(_mask).type


###########################################################################
class PEDA(object):
    """
    Class for actual functions of PEDA commands
    """

    def __init__(self):
        self.SAVED_COMMANDS = {}  # saved GDB user's commands
        self.enabled = True

    ####################################
    #   GDB Interaction / Misc Utils   #
    ####################################
    def execute(self, gdb_command, to_string=False):
        """
        Wrapper for gdb.execute, catch the exception so it will not stop python script

        Args:
            - gdb_command (String)

        Returns:
            - True if execution succeed (Bool)
            - Results of command (String)
        """
        try:
            out = gdb.execute(gdb_command, to_string=to_string)
            if to_string:
                return out
            else:
                return True
        except Exception as e:
            if config.Option.get("debug") == "on":
                msg('Exception (%s): %s' % (gdb_command, e), "red")
                traceback.print_stack()
            return False

    def parse_and_eval(self, exp):
        """
        Wrapper for gdb.parse_and_eval
        Only used in parsing asm and cmd argv

        Args:
            - exp: expression to evaluate (String)

        Returns:
            - value of expression (Int)
        """
        (arch, _) = self.getarch()
        if "aarch64" in arch:
            regs = REGISTERS["elf64-littleaarch64"]
        elif "arm" in arch:
            regs = REGISTERS["elf32-littlearm"]
        else:
            regs = REGISTERS["elf64-x86-64"] + REGISTERS["elf32-i386"] + REGISTERS[16] + REGISTERS[8]
        for r in regs:
            if "$" + r not in exp and "e" + r not in exp and "r" + r not in exp:
                exp = exp.replace(r, "$%s" % r)

        try:
            return int(gdb.parse_and_eval(exp).cast(_mask_val_type)) # no harm to cast to the longest type
        except gdb.error:
            return None

    def string_to_argv(self, str_arg):
        """
        Convert a string to argv list, pre-processing register and variable values

        Args:
            - str_arg: input string (String)

        Returns:
            - argv list (List)
        """
        try:
            str_arg = str_arg.encode('ascii', 'ignore')
        except:
            pass
        args = list(map(lambda x: utils.decode_string_escape(x), shlex.split(str_arg.decode())))
        # need more processing here
        for idx, arg in enumerate(args):
            arg = arg.strip(",")
            if arg.startswith("+"):  # relative value to prev arg
                adder = self.parse_and_eval(arg[1:])
                if adder is not None:
                    args[idx] = to_hex(to_int(args[idx - 1]) + adder)
            elif '$' in arg or utils.is_math_exp(arg):
                v = self.parse_and_eval(arg)
                if v is not None:
                    args[idx] = str(v)
        if config.Option.get("verbose") == "on":
            msg(args)
        return args

    ################################
    #   GDB User-Defined Helpers   #
    ################################
    def save_user_command(self, cmd):
        """
        Save user-defined command and deactivate it

        Args:
            - cmd: user-defined command (String)

        Returns:
            - True if success to save (Bool)
        """
        commands = self.execute("show user %s" % cmd, to_string=True)
        if not commands:
            return False

        commands = "\n".join(commands.splitlines()[1:])
        commands = "define %s\n" % cmd + commands + "end\n"
        self.SAVED_COMMANDS[cmd] = commands
        tmp = utils.tmpfile()
        tmp.write("define %s\nend\n" % cmd)
        tmp.flush()
        result = self.execute("source %s" % tmp.name)
        tmp.close()
        return result

    def define_user_command(self, cmd, code):
        """
        Define a user-defined command, overwrite the old content

        Args:
            - cmd: user-defined command (String)
            - code: gdb script code to append (String)

        Returns:
            - True if success to define (Bool)
        """
        commands = "define %s\n" % cmd + code + "\nend\n"
        tmp = utils.tmpfile(is_binary_file=False)
        tmp.write(commands)
        tmp.flush()
        result = self.execute("source %s" % tmp.name)
        tmp.close()
        return result

    def append_user_command(self, cmd, code):
        """
        Append code to a user-defined command, define new command if not exist

        Args:
            - cmd: user-defined command (String)
            - code: gdb script code to append (String)

        Returns:
            - True if success to append (Bool)
        """
        commands = self.execute("show user %s" % cmd, to_string=True)
        if not commands:
            return self.define_user_command(cmd, code)
        # else
        commands = "\n".join(commands.splitlines()[1:])
        if code in commands:
            return True

        commands = "define %s\n" % cmd + commands + code + "\nend\n"
        tmp = utils.tmpfile()
        tmp.write(commands)
        tmp.flush()
        result = self.execute("source %s" % tmp.name)
        tmp.close()
        return result

    def restore_user_command(self, cmd):
        """
        Restore saved user-defined command

        Args:
            - cmd: user-defined command (String)

        Returns:
            - True if success to restore (Bool)
        """
        if cmd == "all":
            commands = "\n".join(self.SAVED_COMMANDS.values())
            self.SAVED_COMMANDS = {}
        else:
            if cmd not in self.SAVED_COMMANDS:
                return False
            else:
                commands = self.SAVED_COMMANDS[cmd]
                self.SAVED_COMMANDS.pop(cmd)
        tmp = utils.tmpfile()
        tmp.write(commands)
        tmp.flush()
        result = self.execute("source %s" % tmp.name)
        tmp.close()

        return result

    def run_gdbscript_code(self, code):
        """
        Run basic gdbscript code as it is typed in interactively

        Args:
            - code: gdbscript code, lines are splitted by "\n" or ";" (String)

        Returns:
            - True if success to run (Bool)
        """
        tmp = utils.tmpfile()
        tmp.write(code.replace(";", "\n"))
        tmp.flush()
        result = self.execute("source %s" % tmp.name)
        tmp.close()
        return result

    #########################
    #   Debugging Helpers   #
    #########################
    @memoized
    def is_target_remote(self):
        """
        Check if current target is remote

        Returns:
            - True if target is remote (Bool)
        """
        out = self.execute("info program", to_string=True)
        if out and "serial line" in out:  # remote target
            return True

        return False

    @memoized
    def getfile(self):
        """
        Get exec file of debugged program

        Returns:
            - full path to executable file (String)
        """
        result = None
        out = self.execute('info files', to_string=True)
        if out and '"' in out:
            m = re.search(".*exec file:\s*`(.*)'", out)
            if m:
                result = m.group(1)
            else:  # stripped file, get symbol file
                m = re.search("Symbols from \"([^\"]*)", out)
                if m:
                    result = m.group(1)

        return result

    def get_status(self):
        """
        Get execution status of debugged program

        Returns:
            - current status of program (String)
                STOPPED - not being run
                BREAKPOINT - breakpoint hit
                SIGXXX - stopped by signal XXX
                UNKNOWN - unknown, not implemented
        """
        status = "UNKNOWN"
        out = self.execute("info program", to_string=True)
        for line in out.splitlines():
            if line.startswith("It stopped"):
                if "signal" in line:  # stopped by signal
                    status = line.split("signal")[1].split(",")[0].strip()
                    break
                if "breakpoint" in line:  # breakpoint hit
                    status = "BREAKPOINT"
                    break
            if "not being run" in line:
                status = "STOPPED"
                break
        return status

    @memoized
    def getpid(self):
        """
        Get PID of the debugged process

        Returns:
            - pid (Int)
        """
        status = self.get_status()
        if not status or status == "STOPPED":
            return None
        pid = gdb.selected_inferior().pid
        return int(pid) if pid else None

    def getos(self):
        """
        Get running OS info

        Returns:
            - os version (String)
        """
        # TODO: get remote os by calling uname()
        return os.uname()[0]

    @memoized
    def getarch(self):
        """
        Get architecture of debugged program

        Returns:
            - tuple of architecture info (arch (String), bits (Int))
        """
        gdb_arch = self.execute('show architecture', to_string=True)
        if 'i386:x86-64' in gdb_arch:
            return ('elf64-x86-64', 64)
        elif 'i386' in gdb_arch:
            return ('elf32-i386', 32)
        elif 'arm' in gdb_arch:
            return ('elf32-littlearm', 32)
        elif 'aarch64' in gdb_arch:
            return ('elf64-littleaarch64', 64)
        arch = "unknown"
        bits = 32
        out = self.execute('maintenance info sections ?', to_string=True).splitlines()
        for line in out:
            if "file type" in line:
                arch = line.split()[-1][:-1]
                break
        if "64" in arch:
            bits = 64
        return (arch, bits)

    @memoized
    def intsize(self):
        """
        Get dword size of debugged program

        Returns:
            - size (Int)
                + intsize = 4/8 for 32/64-bits arch
        """
        (_, bits) = self.getarch()
        return bits // 8

    def unpack(self, s, intsize):
        """
        Unpack a string to unsigned interger according to intsize

        Returns:
            - Unpack result (Int)
        """
        if intsize == 8:
            return u64(s)
        elif intsize == 4:
            return u32(s)

    def pack(self, n, intsize):
        """
        Pack a unsigned interger to string according to intsize

        Returns:
            - Pack result (String)
        """
        if intsize == 8:
            return p64(n)
        elif intsize == 4:
            return p32(n)

    def getregs(self, reglist=None):
        """
        Get value of some or all registers

        Returns:
            - dictionary of {regname(String) : value(Int)}
        """
        if reglist:
            reglist = reglist.replace(",", " ")
        else:
            reglist = ""
        regs = self.execute("info registers %s" % reglist, to_string=True)
        if not regs:
            return None

        result = {}
        (arch, bits) = self.getarch()
        if regs:
            if "mips" in arch:
                tmp = regs.splitlines()
                klist = tmp[0].split() + tmp[2].split() + tmp[4].split() + tmp[6].split() + tmp[8].split(
                ) + tmp[10].split()
                vlist = tmp[1].split()[1:] + tmp[3].split()[1:] + tmp[5].split()[1:] + tmp[7].split(
                )[1:] + tmp[9].split() + tmp[11].split()
                vlist = [to_int("0x" + v) for v in vlist]
                result = dict(zip(klist, vlist))
            else:
                for r in regs.splitlines():
                    r = r.split()
                    if len(r) > 1 and to_int(r[1]) is not None:
                        result[r[0]] = to_int(r[1])

        return result

    def getreg(self, register):
        """
        Get value of a specific register

        Args:
            - register: register name (String)

        Returns:
            - register value (Int)
        """
        r = register.lower()
        regs = self.execute("info registers %s" % r, to_string=True)
        if regs:
            regs = regs.splitlines()
            if len(regs) > 1:
                return None
            else:
                result = to_int(regs[0].split()[1])
                return result

        return None

    def get_breakpoint(self, num):
        """
        Get info of a specific breakpoint
        TODO: support catchpoint, watchpoint

        Args:
            - num: breakpoint number

        Returns:
            - tuple (Num(Int), Type(String), Disp(Bool), Enb(Bool), Address(Int), What(String), commands(String))
        """
        out = self.execute("info breakpoints %d" % num, to_string=True)
        if not out or "No breakpoint" in out:
            return None

        lines = out.splitlines()[1:]
        # breakpoint regex
        m = re.match("^(\d*)\s*(.*breakpoint)\s*(keep|del)\s*(y|n)\s*(0x\S+)\s*(.*)", lines[0])
        if not m:
            # catchpoint/watchpoint regex
            m = re.match("^(\d*)\s*(.*point)\s*(keep|del)\s*(y|n)\s*(.*)", lines[0])
            if not m:
                return None
            else:
                (num, type, disp, enb, what) = m.groups()
                addr = ''
        else:
            (num, type, disp, enb, addr, what) = m.groups()

        disp = True if disp == "keep" else False
        enb = True if enb == "y" else False
        addr = to_int(addr)
        m = re.match("in.*at(.*:\d*)", what)
        if m:
            what = m.group(1)
        else:
            if addr:  # breakpoint
                what = ""

        commands = ""
        if len(lines) > 1:
            for line in lines[1:]:
                if "already hit" in line: continue
                commands += line + "\n"

        return (num, type, disp, enb, addr, what, commands.rstrip())

    def get_breakpoints(self):
        """
        Get list of current breakpoints

        Returns:
            - list of tuple (Num(Int), Type(String), Disp(Bool), Nnb(Bool), Address(Int), commands(String))
        """
        result = []
        out = self.execute("info breakpoints", to_string=True)
        if not out:
            return []

        bplist = []
        for line in out.splitlines():
            m = re.match("^(\d*).*", line)
            if m and to_int(m.group(1)):
                bplist += [to_int(m.group(1))]

        for num in bplist:
            r = self.get_breakpoint(num)
            if r:
                result += [r]
        return result

    def save_breakpoints(self, filename):
        """
        Save current breakpoints to file as a script

        Args:
            - filename: target file (String)

        Returns:
            - True if success to save (Bool)
        """
        # use built-in command for gdb 7.2+
        result = self.execute("save breakpoints %s" % filename, to_string=True)
        if result == '':
            return True

        bplist = self.get_breakpoints()
        if not bplist:
            return False

        try:
            fd = open(filename, "w")
            for (num, type, disp, enb, addr, what, commands) in bplist:
                m = re.match("(.*)point", type)
                if m:
                    cmd = m.group(1).split()[-1]
                else:
                    cmd = "break"
                if "hw" in type and cmd == "break":
                    cmd = "h" + cmd
                if "read" in type:
                    cmd = "r" + cmd
                if "acc" in type:
                    cmd = "a" + cmd

                if not disp:
                    cmd = "t" + cmd
                if what:
                    location = what
                else:
                    location = "*%#x" % addr
                text = "%s %s" % (cmd, location)
                if commands:
                    if "stop only" not in commands:
                        text += "\ncommands\n%s\nend" % commands
                    else:
                        text += commands.split("stop only", 1)[1]
                fd.write(text + "\n")
            fd.close()
            return True
        except:
            return False

    def get_config_filename(self, name):
        filename = self.getfile()
        if not filename:
            filename = self.getpid()
            if not filename:
                filename = 'unknown'

        filename = os.path.basename("%s" % filename)
        tmpl_name = config.Option.get(name)
        if tmpl_name:
            return tmpl_name.replace("#FILENAME#", filename)
        else:
            return "peda-%s-%s" % (name, filename)

    def save_session(self, filename=None):
        """
        Save current working gdb session to file as a script

        Args:
            - filename: target file (String)

        Returns:
            - True if success to save (Bool)
        """
        session = ""
        if not filename:
            filename = self.get_config_filename("session")

        # exec-wrapper
        out = self.execute("show exec-wrapper", to_string=True)
        wrapper = out.split('"')[1]
        if wrapper:
            session += "set exec-wrapper %s\n" % wrapper

        try:
            # save breakpoints
            self.save_breakpoints(filename)
            fd = open(filename, "a+")
            fd.write("\n" + session)
            fd.close()
            return True
        except:
            return False

    def restore_session(self, filename=None):
        """
        Restore previous saved working gdb session from file

        Args:
            - filename: source file (String)

        Returns:
            - True if success to restore (Bool)
        """
        if not filename:
            filename = self.get_config_filename("session")

        # temporarily save and clear breakpoints
        tmp = utils.tmpfile()
        self.save_breakpoints(tmp.name)
        self.execute("delete")
        result = self.execute("source %s" % filename)
        if not result:
            self.execute("source %s" % tmp.name)
        tmp.close()
        return result

    @memoized
    def assemble(self, asmcode, bits=None):
        """
        Assemble ASM instructions using NASM
            - asmcode: input ASM instructions, multiple instructions are separated by ";" (String)

        Returns:
            - bin code (raw bytes)
        """
        if bits is None:
            (arch, bits) = self.getarch()
        return Nasm.assemble(asmcode, bits)

    def disassemble(self, *arg):
        """
        Wrapper for disassemble command
            - arg: args for disassemble command

        Returns:
            - text code (String)
        """
        code = ""
        modif = ""
        arg = list(arg)
        if len(arg) > 1:
            if "/" in arg[0]:
                modif = arg[0]
                arg = arg[1:]
        if len(arg) == 1 and to_int(arg[0]) is not None:
            arg += [to_hex(to_int(arg[0]) + 32)]

        self.execute("set disassembly-flavor intel")
        out = self.execute("disassemble %s %s" % (modif, ",".join(arg)), to_string=True)
        if not out:
            return None
        else:
            code = out

        return code

    @memoized
    def prev_inst(self, address, count=1):
        """
        Get previous instructions at an address

        Args:
            - address: address to get previous instruction (Int)
            - count: number of instructions to read (Int)

        Returns:
            - list of tuple (address(Int), code(String))
        """
        result = []
        backward = 64 + 16 * count
        for i in range(backward):
            if self.getpid() and not self.is_address(address - backward + i):
                continue

            code = self.execute("disassemble %s, %s" % (to_hex(address - backward + i), to_hex(address + 1)),
                                to_string=True)
            if code and ("%x" % address) in code:
                lines = code.strip().splitlines()[1:-1]
                if len(lines) > count and all(["(bad)" not in _l for _l in lines]):
                    for line in lines[-count - 1:-1]:
                        try:
                            (addr, code) = line.split(":", 1)
                        except ValueError:
                            warning_msg('asm code error at {:#x}, line: {}'.format(address, line))
                            continue
                        addr = re.search("(0x\S+)", addr).group(1)
                        result += [(to_int(addr), code)]
                    return result
        return None

    @memoized
    def current_inst(self, address):
        """
        Parse instruction at an address

        Args:
            - address: address to get next instruction (Int)

        Returns:
            - tuple of (address(Int), code(String))
        """
        out = self.execute("x/i %#x" % address, to_string=True)
        if not out:
            return None

        (addr, code) = out.split(":", 1)
        addr = re.search("(0x\S+)", addr).group(1)
        addr = to_int(addr)
        code = code.strip()

        return (addr, code)

    @memoized
    def next_inst(self, address, count=1):
        """
        Get next instructions at an address

        Args:
            - address: address to get next instruction (Int)
            - count: number of instructions to read (Int)

        Returns:
            - - list of tuple (address(Int), code(String))
        """
        result = []
        code = self.execute("x/%di %#x" % (count + 1, address), to_string=True)
        if not code:
            return None

        lines = code.strip().splitlines()
        for i in range(1, count + 1):
            if ":" not in lines[i]:
                i += 1
            (addr, code) = lines[i].split(":", 1)
            addr = re.search("(0x\S+)", addr).group(1)
            result += [(to_int(addr), code)]
        return result

    @memoized
    def disassemble_around(self, address, count=8):
        """
        Disassemble instructions nearby current PC or an address

        Args:
            - address: start address to disassemble around (Int)
            - count: number of instructions to disassemble

        Returns:
            - text code (String)
        """
        count = min(count, 256)
        pc = address
        if pc is None:
            return None

        # check if address is reachable
        if self.read_int(pc) is None:
            return None

        prev_code = self.prev_inst(pc, count // 2 - 1)
        if prev_code:
            start = prev_code[0][0]
        else:
            start = pc
        if start == pc:
            count = count // 2

        code = self.execute("x/%di %#x" % (count, start), to_string=True)
        if "%#x" % pc not in code:
            code = self.execute("x/%di %#x" % (count // 2, pc), to_string=True)

        return code.rstrip()

    @memoized
    def xrefs(self, search="", filename=None):
        """
        Search for all call references or data access to a function/variable

        Args:
            - search: function or variable to search for (String)
            - filename: binary/library to search (String)

        Returns:
            - list of tuple (address(Int), asm instruction(String))
        """
        result = []
        if not filename:
            filename = self.getfile()

        if not filename:
            return None
        vmap = self.get_vmmap(filename)
        elfbase = vmap[0][0] if vmap else 0

        if to_int(search) is not None:
            search = "%x" % to_int(search)

        search_data = 1
        if search == "":
            search_data = 0

        out = utils.execute_external_command("%s -M intel -z --prefix-address -d '%s' | grep '%s'" %
                                             (config.OBJDUMP, filename, search))

        for line in out.splitlines():
            if not line: continue
            addr = to_int("0x" + line.split()[0].strip())
            if not addr: continue

            # update with runtime values
            if addr < elfbase:
                addr += elfbase
            out = self.execute("x/i %#x" % addr, to_string=True)
            if out:
                line = out
                m = re.search("\s*(0x\S+).*?:\s*([^ ]*)\s*(.*)", line)
            else:
                m = re.search("(.*?)\s*<.*?>\s*([^ ]*)\s*(.*)", line)

            if m:
                (address, opcode, opers) = m.groups()
                if "call" in opcode and search in opers:
                    result += [(addr, line.strip())]
                if search_data:
                    if "mov" in opcode and search in opers:
                        result += [(addr, line.strip())]

        return result

    def _get_function_args_32(self, code, argc=None):
        """
        Guess the number of arguments passed to a function - i386
        """
        if not argc:
            argc = 0
            matches = re.findall(".*mov.*\[esp(.*)\],", code)
            if matches:
                l = len(matches)
                for v in matches:
                    if v.startswith("+"):
                        offset = to_int(v[1:])
                        if offset is not None and (offset // 4) > l:
                            continue
                    argc += 1
            else:  # try with push style
                argc = code.count("push")

        argc = min(argc, 6)
        if argc == 0:
            return []

        args = []
        sp = self.getreg("sp")
        for i in range(argc):
            args.append(self.read_int(sp + i * 4, 4))

        return args

    def _get_function_args_64(self, code, argc=None):
        """
        Guess the number of arguments passed to a function - x86_64
        """
        # just retrieve max 6 args
        arg_order = ["rdi", "rsi", "rdx", "rcx", "r8", "r9"]
        matches = re.findall(":\s*([^ ]*)\s*(.*),", code)
        regs = [r for (_, r) in matches]
        m = re.findall(("di|si|dx|cx|r8|r9"), " ".join(regs))
        m = list(set(m))  # uniqify
        argc = 0
        if "si" in m and "di" not in m:  # dirty fix
            argc += 1
        argc += m.count("di")
        if argc > 0:
            argc += m.count("si")
        if argc > 1:
            argc += m.count("dx")
        if argc > 2:
            argc += m.count("cx")
        if argc > 3:
            argc += m.count("r8")
        if argc > 4:
            argc += m.count("r9")

        if argc == 0:
            return []

        args = []
        regs = self.getregs()
        for i in range(argc):
            args += [regs[arg_order[i]]]

        return args

    def _get_function_args_arm(self, code, argc=None):
        """
        Guess the number of arguments passed to a function - aarch64
        """
        # just retrieve max 6 args
        arg_order = ["r0", "r1", "r2", "r3", "r4", "r5"]
        matches = re.findall(":\s*([^\s]*)\s*([^,]*)", code)
        regs = [r for (_, r) in matches]
        m = re.findall("(r[0-5])", " ".join(regs))
        m = list(set(m))  # uniqify
        argc = 0
        if "r1" in m and "r0" not in m:  # dirty fix
            argc += 1
        argc += m.count("r0")
        if argc > 0:
            argc += m.count("r1")
        if argc > 1:
            argc += m.count("r2")
        if argc > 2:
            argc += m.count("r3")
        if argc > 3:
            argc += m.count("r4")
        if argc > 4:
            argc += m.count("r5")

        if argc == 0:
            return []

        args = []
        regs = self.getregs()
        for i in range(argc):
            args += [regs[arg_order[i]]]

        return args

    def _get_function_args_aarch64(self, code, argc=None):
        """
        Guess the number of arguments passed to a function - aarch64
        """
        # just retrieve max 6 args
        arg_order = ["x0", "x1", "x2", "x3", "x4", "x5"]
        matches = re.findall(":\s*([^\s]*)\s*([^,]*)", code)
        regs = [r for (_, r) in matches]
        m = re.findall("(x[0-5]|w[0-5])", " ".join(regs))
        m = list(set(m))  # uniqify
        argc = 0
        if "x1" in m and "x0" not in m:  # dirty fix
            argc += 1
        argc += m.count("x0")
        if argc > 0:
            if m.count("x1") != 0:
                argc += m.count("x1")
            else:
                argc += m.count("w1")
        if argc > 1:
            if m.count("x2") != 0:
                argc += m.count("x2")
            else:
                argc += m.count("w2")
        if argc > 2:
            if m.count("x3") != 0:
                argc += m.count("x3")
            else:
                argc += m.count("w3")
        if argc > 3:
            if m.count("x4") != 0:
                argc += m.count("x4")
            else:
                argc += m.count("w4")
        if argc > 4:
            if m.count("x5") != 0:
                argc += m.count("x5")
            else:
                argc += m.count("w5")
        if argc == 0:
            return []

        args = []
        regs = self.getregs()
        for i in range(argc):
            args += [regs[arg_order[i]]]

        return args

    def _get_function_args_ppc(self, code, argc=None):
        """
        Guess the number of arguments passed to a function - aarch64
        """
        # just retrieve max 6 args
        arg_order = ["r3", "r4", "r5", "r6", "r7", "r8"]
        matches = re.findall(":\s*([^\s]*)\s*([^,]*)", code)
        matches = p.findall(code)
        regs = [r for (_, r) in matches]
        m = re.findall("(r[0-5])", " ".join(regs))
        m = list(set(m))  # uniqify
        argc = 0
        if "r4" in m and "r3" not in m:  # dirty fix
            argc += 1
        argc += m.count("r3")
        if argc > 0:
            argc += m.count("r4")
        if argc > 1:
            argc += m.count("r5")
        if argc > 2:
            argc += m.count("r6")
        if argc > 3:
            argc += m.count("r7")
        if argc > 4:
            argc += m.count("r8")

        if argc == 0:
            return []

        args = []
        regs = self.getregs()
        for i in range(argc):
            args += [regs[arg_order[i]]]

        return args

    def get_function_args(self, argc=None):
        """
        Get the guessed arguments passed to a function when stopped at a call instruction

        Args:
            - argc: force to get specific number of arguments (Int)

        Returns:
            - list of arguments (List)
        """
        args = []
        regs = self.getregs()
        if regs is None:
            return []

        (arch, bits) = self.getarch()
        pc = self.getreg("pc")
        prev_insts = self.prev_inst(pc, 12)

        code = ""
        if not prev_insts:
            return []
        if "aarch64" in arch:
            for (addr, inst) in prev_insts[::-1]:
                if "bl" in inst.strip().split()[0]:
                    break
                code = "%#x:%s\n" % (addr, inst) + code
        else:
            for (addr, inst) in prev_insts[::-1]:
                if "call" in inst.strip().split()[0]:
                    break
                code = "%#x:%s\n" % (addr, inst) + code

        if "aarch64" in arch:
            args = self._get_function_args_aarch64(code, argc)
        elif "i386" in arch:
            args = self._get_function_args_32(code, argc)
        elif "64" in arch:
            args = self._get_function_args_64(code, argc)
        elif "arm" in arch:
            args = self._get_function_args_arm(code, argc)
        elif "powerpc" in arch:
            args = self._get_function_args_ppc(code, argc)

        return args

    @memoized
    def backtrace_depth(self, sp=None):
        """
        Get number of frames in backtrace

        Args:
            - sp: stack pointer address, for caching (Int)

        Returns:
            - depth: number of frames (Int)
        """
        backtrace = self.execute("backtrace", to_string=True)
        return backtrace.count("#")

    def stepuntil(self, inst, mapname=None, depth=None):
        """
        Step execution until next "inst" instruction within a specific memory range

        Args:
            - inst: the instruction to reach (String)
            - mapname: name of virtual memory region to check for the instruction (String)
            - depth: backtrace depth (Int)

        Returns:
            - tuple of (depth, instruction)
                + depth: current backtrace depth (Int)
                + instruction: current instruction (String)
        """
        if not self.getpid():
            return None

        maxdepth = to_int(config.Option.get("tracedepth"))
        if not maxdepth:
            maxdepth = 0xffffffff

        maps = self.get_vmmap()
        binname = self.getfile()
        if mapname is None:
            mapname = binname
        mapname = mapname.replace(" ", "").split(",") + [binname]
        targetmap = []
        for m in mapname:
            targetmap += self.get_vmmap(m)
        binmap = self.get_vmmap("binary")

        current_instruction = ""
        pc = self.getreg("pc")

        if depth is None:
            current_depth = self.backtrace_depth(self.getreg("sp"))
        else:
            current_depth = depth
        old_status = self.get_status()

        while True:
            status = self.get_status()
            if status != old_status:
                if "SIG" in status and status[3:] not in [
                        "TRAP"
                ] and not to_int(status[3:]):  # ignore TRAP and numbered signals
                    current_instruction = "Interrupted: %s" % status
                    call_depth = current_depth
                    break
                if "STOP" in status:
                    current_instruction = "End of execution"
                    call_depth = current_depth
                    break

            call_depth = self.backtrace_depth(self.getreg("sp"))
            current_instruction = self.execute("x/i $pc", to_string=True)
            if not current_instruction:
                current_instruction = "End of execution"
                break

            addr = re.search(".*?(0x[^ :]*)", current_instruction).group(1)
            addr = to_int(addr)
            if addr is None:
                break

            code = re.match(".*?:\s*(.*)", current_instruction).group(1)
            found = 0
            for i in inst.replace(",", " ").split():
                if re.match(i.strip(), code.strip()):
                    if self.is_address(addr, targetmap) and addr != pc:
                        found = 1
                        break
            if found != 0:
                break
            self.execute("stepi", to_string=True)
            if not self.is_address(addr, targetmap) or call_depth > maxdepth:
                self.execute("finish", to_string=True)
            pc = 0

        return (call_depth - current_depth, current_instruction.strip())

    def get_eflags(self):
        """
        Get flags value from EFLAGS register

        Returns:
            - dictionary of named flags
        """
        # Eflags bit masks, source vdb
        EFLAGS_CF = 1 << 0
        EFLAGS_PF = 1 << 2
        EFLAGS_AF = 1 << 4
        EFLAGS_ZF = 1 << 6
        EFLAGS_SF = 1 << 7
        EFLAGS_TF = 1 << 8
        EFLAGS_IF = 1 << 9
        EFLAGS_DF = 1 << 10
        EFLAGS_OF = 1 << 11

        flags = {"CF": 0, "PF": 0, "AF": 0, "ZF": 0, "SF": 0, "TF": 0, "IF": 0, "DF": 0, "OF": 0}
        eflags = self.getreg("eflags")
        if not eflags:
            return None
        flags["CF"] = bool(eflags & EFLAGS_CF)
        flags["PF"] = bool(eflags & EFLAGS_PF)
        flags["AF"] = bool(eflags & EFLAGS_AF)
        flags["ZF"] = bool(eflags & EFLAGS_ZF)
        flags["SF"] = bool(eflags & EFLAGS_SF)
        flags["TF"] = bool(eflags & EFLAGS_TF)
        flags["IF"] = bool(eflags & EFLAGS_IF)
        flags["DF"] = bool(eflags & EFLAGS_DF)
        flags["OF"] = bool(eflags & EFLAGS_OF)

        return flags

    def get_cpsr(self):
        """
        Get flags value from CPSR register

        Reurns :
            - dictionary of named flags
        """
        CPSR_N = 1 << 0x1f
        CPSR_Z = 1 << 0x1e
        CPSR_C = 1 << 0x1d
        CPSR_V = 1 << 0x1c
        CPSR_Q = 1 << 0x1b
        CPSR_J = 1 << 0x18
        CPSR_GE = 7 << 0x10
        CPSR_E = 1 << 9
        CPSR_A = 1 << 8
        CPSR_I = 1 << 7
        CPSR_F = 1 << 6
        CPSR_T = 1 << 5

        flags = {"T": 0, "F": 0, "I": 0, "A": 0, "E": 0, "GE": 0, "J": 0, "Q": 0, "V": 0, "C": 0, "Z": 0, "N": 0}
        cpsr = self.getreg("cpsr")
        if cpsr is None:
            return None
        flags["T"] = bool(cpsr & CPSR_T)
        flags["F"] = bool(cpsr & CPSR_F)
        flags["I"] = bool(cpsr & CPSR_I)
        flags["A"] = bool(cpsr & CPSR_A)
        flags["E"] = bool(cpsr & CPSR_E)
        flags["GE"] = bool(cpsr & CPSR_GE)
        flags["J"] = bool(cpsr & CPSR_J)
        flags["Q"] = bool(cpsr & CPSR_Q)
        flags["V"] = bool(cpsr & CPSR_V)
        flags["C"] = bool(cpsr & CPSR_C)
        flags["Z"] = bool(cpsr & CPSR_Z)
        flags["N"] = bool(cpsr & CPSR_N)

        return flags

    def get_aarch64_cpsr(self):
        """
        Get flags value from CPSR register

        Reurns :
            - dictionary of named flags
        """
        CPSR_N = 1 << 0x1f
        CPSR_Z = 1 << 0x1e
        CPSR_C = 1 << 0x1d
        CPSR_V = 1 << 0x1c
        CPSR_D = 1 << 9
        CPSR_A = 1 << 8
        CPSR_I = 1 << 7
        CPSR_F = 1 << 6

        flags = {"F": 0, "I": 0, "A": 0, "D": 0, "V": 0, "C": 0, "Z": 0, "N": 0}
        cpsr = self.getreg("cpsr")
        if cpsr is None:
            return None
        flags["F"] = bool(cpsr & CPSR_F)
        flags["I"] = bool(cpsr & CPSR_I)
        flags["A"] = bool(cpsr & CPSR_A)
        flags["D"] = bool(cpsr & CPSR_D)
        flags["V"] = bool(cpsr & CPSR_V)
        flags["C"] = bool(cpsr & CPSR_C)
        flags["Z"] = bool(cpsr & CPSR_Z)
        flags["N"] = bool(cpsr & CPSR_N)

        return flags

    def set_eflags(self, flagname, value):
        """
        Set/clear/toggle value of a flag register

        Returns:
            - True if success (Bool)
        """
        # Eflags bit masks, source vdb
        EFLAGS_CF = 1 << 0
        EFLAGS_PF = 1 << 2
        EFLAGS_AF = 1 << 4
        EFLAGS_ZF = 1 << 6
        EFLAGS_SF = 1 << 7
        EFLAGS_TF = 1 << 8
        EFLAGS_IF = 1 << 9
        EFLAGS_DF = 1 << 10
        EFLAGS_OF = 1 << 11

        flags = {
            "carry": "CF",
            "parity": "PF",
            "adjust": "AF",
            "zero": "ZF",
            "sign": "SF",
            "trap": "TF",
            "interrupt": "IF",
            "direction": "DF",
            "overflow": "OF"
        }

        flagname = flagname.lower()

        if flagname not in flags:
            return False

        eflags = self.get_eflags()
        if not eflags:
            return False

        # If value doesn't match the current, or we want to toggle, toggle
        if value is None or eflags[flags[flagname]] != value:
            reg_eflags = self.getreg("eflags")
            reg_eflags ^= eval("EFLAGS_%s" % flags[flagname])
            result = self.execute("set $eflags = %#x" % reg_eflags)
            return result

        return True

    def eval_target(self, opcode, inst):
        """
        Evaluate target address of an instruction, used for jumpto decision

        Args:
            - opcode: opcode of the ASM instruction (String)
            - inst: ASM instruction text (String)

        Returns:
            - target address (Int)
        """
        # good for rop dev
        if "ret" in opcode:
            (arch, _) = self.getarch()
            if "aarch64" in arch:
                return self.getreg("x30")
            else:
                sp = self.getreg("sp")
                return self.read_int(sp)

        # this regex includes x86_64 RIP relateive address reference
        # e.g QWORD PTR ds:0xdeadbeef / DWORD PTR [ebx+0xc]
        # TODO: improve this regex
        m = re.search("\w+\s+(\w+) PTR (\[(\S+)\]|\w+:(0x\S+))", inst)
        if m:
            prefix = m.group(1)
            if '[' in m.group(2):
                dest = m.group(3)
                if "rip" in dest:
                    pc = self.getreg("pc")
                    ins_size = self.next_inst(pc)[0][0] - pc
                    dest += "+%d" % ins_size
            else:
                dest = m.group(4)

            if prefix == 'QWORD':
                intsize = 8
            elif prefix == 'DWORD':
                intsize = 4
            elif prefix == 'WORD':
                intsize = 2

            addr = self.parse_and_eval(dest)
            return self.read_int(addr, intsize)

        # e.g. <puts+65>:	je     0x7ffff7e39570 <puts+336>
        #   or <__GI___overflow+73>:	jmp    rax
        m = re.search("\w+\s+(0x\S+|\w+)", inst)
        if m:
            return self.parse_and_eval(m.group(1))

        return None

    def testjump(self, opcode, inst):
        """
        Test if jump instruction is taken or not

        Returns:
            - (status, address of target jumped instruction)
        """
        flags = self.get_eflags()
        if not flags:
            return False, None

        next_addr = self.eval_target(opcode, inst)
        if next_addr is None:
            next_addr = 0

        if (
            opcode == "ret" or
            opcode == "jmp" or
            (opcode == "je" and flags["ZF"]) or
            (opcode == "jne" and not flags["ZF"]) or
            (opcode == "jg" and not flags["ZF"] and flags["SF"] == flags["OF"]) or
            (opcode == "jge" and flags["SF"] == flags["OF"]) or
            (opcode == "ja" and not flags["CF"] and not flags["ZF"]) or
            (opcode == "jae" and not flags["CF"]) or
            (opcode == "jl" and flags["SF"] != flags["OF"]) or
            (opcode == "jle" and (flags["ZF"] or flags["SF"] != flags["OF"])) or
            (opcode == "jb" and flags["CF"]) or
            (opcode == "jbe" and (flags["CF"] or flags["ZF"])) or
            (opcode == "jo" and flags["OF"]) or
            (opcode == "jno" and not flags["OF"]) or
            (opcode == "jz" and flags["ZF"]) or
            (opcode == "jnz" and flags["OF"])
        ):
            return True, next_addr

        return False, None

    def aarch64_testjump(self, opcode, inst):
        """
        Test if jump instruction is taken or not - aarch64

        Returns:
            - (status, address of target jumped instruction)
        """
        flags = self.get_aarch64_cpsr()
        if not flags:
            return False, None

        next_addr = self.eval_target(opcode, inst)
        if next_addr is None:
            next_addr = 0

        if (
            "ret" in opcode or
            opcode == "b" or
            (opcode == "b.eq" and flags["Z"]) or
            (opcode == "b.ne" and not flags["Z"]) or
            (opcode == "b.cs" and flags["C"]) or
            (opcode == "b.cc" and not flags["C"]) or
            (opcode == "b.mi" and flags["N"]) or
            (opcode == "b.pl" and not flags["N"]) or
            (opcode == "b.vs" and flags["O"]) or
            (opcode == "b.vc" and not flags["O"]) or
            (opcode == "b.hi" and not flags["Z"] and flags["C"]) or
            (opcode == "b.ls" and not flags["C"] and flags["Z"]) or
            (opcode == "b.ge" and flags["N"] == flags["V"]) or
            (opcode == "b.lt" and flags["N"] != flags["V"]) or
            (opcode == "b.gt" and not flags["Z"] and flags["N"] == flags["V"]) or
            (opcode == "b.le" and flags["Z"] and flags["N"] != flags["V"])
        ):
            return True, next_addr

        if opcode == "cbnz" or opcode == "cbz":
            rn = inst.split(":\t")[-1].split()[1].strip(",")
            val = self.parse_and_eval(rn)
            if val == 0:
                return True, next_addr

        return False, None

    def arm_testjump(self, opcode, inst):
        """
        Test if jump instruction is taken or not - arm

        Returns:
            - (status, address of target jumped instruction)
        """
        flags = self.get_cpsr()
        if not flags:
            return False, None

        next_addr = self.eval_target(opcode, inst)
        if next_addr is None:
            next_addr = 0

        if (
            opcode == "b" or
            (opcode.startswith("beq") and flags["Z"]) or
            (opcode.startswith("bne") and not flags["Z"]) or
            (opcode.startswith("bcs") and flags["C"]) or
            (opcode.startswith("bcc") and not flags["C"]) or
            (opcode.startswith("bmi") and flags["N"]) or
            (opcode.startswith("bpl") and not flags["N"]) or
            (opcode.startswith("bvs") and flags["O"]) or
            (opcode.startswith("bvc") and not flags["O"]) or
            (opcode.startswith("bhi") and not flags["Z"] and flags["C"]) or
            (opcode.startswith("bls") and not flags["C"] and flags["Z"]) or
            (opcode.startswith("bge") and flags["N"] == flags["V"]) or
            (opcode.startswith("blt") and flags["N"] != flags["V"]) or
            (opcode.startswith("bgt") and not flags["Z"] and flags["N"] == flags["V"]) or
            (opcode.startswith("ble") and flags["Z"] and flags["N"] != flags["V"])
        ):
            return True, next_addr

        if opcode == "cbnz" or opcode == "cbz":
            rn = inst.split(":\t")[-1].split()[1].strip(",")
            val = self.parse_and_eval(rn)
            if val == 0:
                return True, next_addr

        return False, None

    def take_snapshot(self):
        """
        Take a snapshot of current process
        Warning: this is not thread safe, do not use with multithread program

        Returns:
            - dictionary of snapshot data
        """
        if not self.getpid():
            return None

        maps = self.get_vmmap()
        if not maps:
            return None

        snapshot = {}
        # get registers
        snapshot["reg"] = self.getregs()
        # get writable memory regions
        snapshot["mem"] = {}
        for (start, end, perm, _) in maps:
            if "w" in perm:
                snapshot["mem"][start] = self.dumpmem(start, end)

        return snapshot

    def save_snapshot(self, filename=None):
        """
        Save a snapshot of current process to file
        Warning: this is not thread safe, do not use with multithread program

        Args:
            - filename: target file to save snapshot

        Returns:
            - Bool
        """
        if not filename:
            filename = self.get_config_filename("snapshot")

        snapshot = self.take_snapshot()
        if not snapshot:
            return False
        # dump to file
        fd = open(filename, "wb")
        pickle.dump(snapshot, fd, pickle.HIGHEST_PROTOCOL)
        fd.close()

        return True

    def give_snapshot(self, snapshot):
        """
        Restore a saved snapshot of current process
        Warning: this is not thread safe, do not use with multithread program

        Returns:
            - Bool
        """
        if not snapshot or not self.getpid():
            return False

        # restore memory regions
        for (addr, buf) in snapshot["mem"].items():
            self.writemem(addr, buf)

        # restore registers, SP will be the last one
        for (r, v) in snapshot["reg"].items():
            self.execute("set $%s = %#x" % (r, v))
            if r.endswith("sp"):
                sp = v
        self.execute("set $sp = %#x" % sp)

        return True

    def restore_snapshot(self, filename=None):
        """
        Restore a saved snapshot of current process from file
        Warning: this is not thread safe, do not use with multithread program

        Args:
            - file: saved snapshot

        Returns:
            - Bool
        """
        if not filename:
            filename = self.get_config_filename("snapshot")

        fd = open(filename, "rb")
        snapshot = pickle.load(fd)
        return self.give_snapshot(snapshot)

    #########################
    #   Memory Operations   #
    #########################
    @memoized
    def get_vmmap(self, name=None):
        """
        Get virtual memory mapping address ranges of debugged process

        Args:
            - name: name/address of binary/library to get mapping range (String)
                + name = "binary" means debugged program
                + name = "all" means all virtual maps

        Returns:
            - list of virtual mapping ranges (start(Int), end(Int), permission(String), mapname(String))
        """

        def _get_section_offset(filename, section):
            out = utils.execute_external_command("%s -W -S %s" % (config.READELF, filename))
            if not out:
                return 0
            # to be improve
            matches = re.findall(".*\[.*\] (\.[^ ]+) [^0-9]* [0-9a-f]+ ([0-9a-f]+).*", out)
            if not matches:
                return 0
            for (hname, off) in matches:
                if hname == section:
                    return to_int('0x' + off)
            return 0

        # credit to https://github.com/pwndbg/pwndbg/blob/88723a8c0a88369fa2dd267d1fb5c63464db55cb/pwndbg/vmmap.py#L274
        def _get_info_files_maps():
            maps = list()
            main_exe = ''
            last_file = list()
            seen_files = set()
            file_maps = self.execute('info files', to_string=True).splitlines()
            if len(file_maps) <= 3:
                return []
            for line in file_maps:
                line = line.strip()
                # The name of the main executable
                if line.startswith('`'):
                    exename, filetype = line.split(None, 1)
                    main_exe = exename.strip("`,'")
                    continue
                # Everything else should be addresses
                if not line.startswith('0x'):
                    continue
                # start, _, stop, _, section, _, filename = line.split(None,6)
                fields = line.split(None, 6)
                if len(fields) == 5: objfile = main_exe
                elif len(fields) == 7: objfile = fields[6]
                else:
                    msg("Bad data: %r" % line)
                    continue
                if objfile not in seen_files:
                    if last_file:
                        end = (to_int(last_file[-1][1]) + 0xfff) & ~0xfff
                        maps.append((start, end, 'rwxp', last_file[-1][0]))
                    seen_files.add(objfile)
                    start = to_int(fields[0]) - _get_section_offset(objfile, fields[4])
                last_file.append((objfile, fields[2]))  # filename and stop addr

            end = (to_int(last_file[-1][1]) + 0xfff) & ~0xfff
            maps.append((start, end, 'rwxp', last_file[-1][0]))
            return maps

        def _get_offline_maps():
            name = self.getfile()
            if not name:
                return None
            headers = self.elfheader()
            binmap = []
            hlist = [x for x in headers.items() if x[1][2] == 'code']
            hlist = sorted(hlist, key=lambda x: x[1][0])
            binmap += [(hlist[0][1][0], hlist[-1][1][1], "rx-p", name)]

            hlist = [x for x in headers.items() if x[1][2] == 'rodata']
            hlist = sorted(hlist, key=lambda x: x[1][0])
            binmap += [(hlist[0][1][0], hlist[-1][1][1], "r--p", name)]

            hlist = [x for x in headers.items() if x[1][2] == 'data']
            hlist = sorted(hlist, key=lambda x: x[1][0])
            binmap += [(hlist[0][1][0], hlist[-1][1][1], "rw-p", name)]

            return binmap

        def _get_allmaps_osx(pid, remote=False):
            maps = []

            if remote:  # remote target, not yet supported
                return maps
            else:  # local target
                try:
                    out = utils.execute_external_command("/usr/bin/vmmap -w %s" % self.getpid())
                except:
                    error_msg("could not read vmmap of process")

            # _DATA                 00007fff77975000-00007fff77976000 [    4K] rw-/rw- SM=COW  /usr/lib/system/libremovefile.dylib
            matches = re.findall("([^\n]*)\s*  ([0-9a-f][^-\s]*)-([^\s]*) \[.*\]\s([^/]*).*  (.*)", out)
            if matches:
                for (name, start, end, perm, mapname) in matches:
                    if name.startswith("Stack"):
                        mapname = "[stack]"
                    start = to_int("0x%s" % start)
                    end = to_int("0x%s" % end)
                    if mapname == "":
                        mapname = name.strip()
                    maps += [(start, end, perm, mapname)]
            return maps

        def _get_allmaps_freebsd(pid, remote=False):
            maps = []
            mpath = "/proc/%s/map" % pid

            if remote:  # remote target, not yet supported
                return maps
            else:  # local target
                try:
                    out = open(mpath).read()
                except:
                    error_msg("could not open %s; is procfs mounted?" % mpath)

            # 0x8048000 0x8049000 1 0 0xc36afdd0 r-x 1 0 0x1000 COW NC vnode /path/to/file NCH -1
            matches = re.findall("0x([0-9a-f]*) 0x([0-9a-f]*)(?: [^ ]*){3} ([rwx-]*)(?: [^ ]*){6} ([^ ]*)", out)
            if matches:
                for (start, end, perm, mapname) in matches:
                    if start[:2] in ["bf", "7f", "ff"] and "rw" in perm:
                        mapname = "[stack]"
                    start = to_int("0x%s" % start)
                    end = to_int("0x%s" % end)
                    if mapname == "-":
                        if start == maps[-1][1] and maps[-1][-1][0] == "/":
                            mapname = maps[-1][-1]
                        else:
                            mapname = "mapped"
                    maps += [(start, end, perm, mapname)]
            return maps

        def _get_allmaps_linux(pid, remote=False):
            maps = []
            mpath = "/proc/%s/maps" % pid

            if remote:  # remote target
                # check if is QEMU
                if 'ENABLE=' in self.execute('maintenance packet Qqemu.sstepbits', to_string=True):
                    # pwndbg uses 'info sharedlibrary' also, but seems it's coverd by 'info files'
                    maps = _get_info_files_maps()
                    # add stack to maps, length is not accurate
                    sp = self.getreg("sp") & ~0xfff
                    maps.append((sp, sp + 0x8000, 'rwxp', '[stack]'))
                    return maps
                else:
                    tmp = utils.tmpfile()
                    self.execute("remote get %s %s" % (mpath, tmp.name))
                    tmp.seek(0)
                    out = tmp.read()
                    tmp.close()
            else:  # local target
                out = open(mpath).read()

            # 00400000-0040b000 r-xp 00000000 08:02 538840  /path/to/file
            matches = re.findall("([0-9a-f]*)-([0-9a-f]*) ([rwxps-]*)(?: [^ ]*){3} *(.*)", out)
            if matches:
                for (start, end, perm, mapname) in matches:
                    start = to_int("0x%s" % start)
                    end = to_int("0x%s" % end)
                    if mapname == "":
                        mapname = "mapped"
                    maps += [(start, end, perm, mapname)]
            return maps

        result = []
        pid = self.getpid()
        if not pid:  # not running, try to use elfheader()
            try:
                return _get_offline_maps()
            except:
                return []

        # retrieve all maps
        os = self.getos()
        rmt = self.is_target_remote()
        maps = []
        try:
            if os == "Linux": maps = _get_allmaps_linux(pid, rmt)
            elif os == "FreeBSD": maps = _get_allmaps_freebsd(pid, rmt)
            elif os == "Darwin": maps = _get_allmaps_osx(pid, rmt)
        except Exception as e:
            if config.Option.get("debug") == "on":
                msg("Exception: %s" % e)
                traceback.print_exc()

        # select maps matched specific name
        if name == "binary":
            name = self.getfile()
        elif name == "heap":
            name = "[heap]"
        if name is None or name == "all":
            name = ""

        if to_int(name) is None:
            for (start, end, perm, mapname) in maps:
                if name in mapname:
                    result += [(start, end, perm, mapname)]
        else:
            addr = to_int(name)
            for (start, end, perm, mapname) in maps:
                if start <= addr and addr < end:
                    result += [(start, end, perm, mapname)]

        return result

    @memoized
    def get_vmrange(self, address, maps=None):
        """
        Get virtual memory mapping range of an address

        Args:
            - address: target address (Int)
            - maps: only find in provided maps (List)

        Returns:
            - tuple of virtual memory info (start, end, perm, mapname)
        """
        if address is None:
            return None
        if maps is None:
            maps = self.get_vmmap()
        if maps:
            for (start, end, perm, mapname) in maps:
                if start <= address and end > address:
                    return (start, end, perm, mapname)
        # failed to get the vmmap
        else:
            try:
                gdb.selected_inferior().read_memory(address, 1)
                start = address & 0xfffffffffffff000
                end = start + 0x1000
                return (start, end, 'rwx', 'unknown')
            except:
                return None

    @memoized
    def is_executable(self, address, maps=None):
        """
        Check if an address is executable

        Args:
            - address: target address (Int)
            - maps: only check in provided maps (List)

        Returns:
            - True if address belongs to an executable address range (Bool)
        """
        vmrange = self.get_vmrange(address, maps)
        if vmrange and "x" in vmrange[2]:
            return True
        else:
            return False

    @memoized
    def is_writable(self, address, maps=None):
        """
        Check if an address is writable

        Args:
            - address: target address (Int)
            - maps: only check in provided maps (List)

        Returns:
            - True if address belongs to a writable address range (Bool)
        """
        vmrange = self.get_vmrange(address, maps)
        if vmrange and "w" in vmrange[2]:
            return True
        else:
            return False

    @memoized
    def is_address(self, value, maps=None):
        """
        Check if a value is a valid address (belongs to a memory region)

        Args:
            - value (Int)
            - maps: only check in provided maps (List)

        Returns:
            - True if value belongs to an address range (Bool)
        """
        vmrange = self.get_vmrange(value, maps)
        return vmrange is not None

    @memoized
    def get_disasm(self, address, count=1):
        """
        Get the ASM code of instruction at address

        Args:
            - address: address to read instruction (Int)
            - count: number of code lines (Int)

        Returns:
            - asm code (String)
        """
        code = self.execute("x/%di %#x" % (count, address), to_string=True)
        if code:
            return code.rstrip()
        else:
            return ""

    def dumpmem(self, start, end):
        """
        Dump process memory from start to end

        Args:
            - start: start address (Int)
            - end: end address (Int)

        Returns:
            - memory content (raw bytes)
        """
        mem = None
        logfd = utils.tmpfile(is_binary_file=True)
        logname = logfd.name
        out = self.execute("dump memory %s %#x %#x" % (logname, start, end), to_string=True)
        if out is None:
            return None
        else:
            logfd.flush()
            mem = logfd.read()
            logfd.close()

        return mem

    def read_mem(self, address, size):
        """
        Read content of memory at an address

        Args:
            - address: start address to read (Int)
            - size: bytes to read (Int)

        Returns:
            - memory content (raw bytes)
        """
        try:
            mem = gdb.selected_inferior().read_memory(address, size).tobytes()
        except gdb.MemoryError:
            return None
        return mem

    def read_int(self, address, intsize=None):
        """
        Read an interger value from memory

        Args:
            - address: address to read (Int)
            - intsize: force read size (Int)

        Returns:
            - mem value (Int)
        """
        if not intsize:
            intsize = self.intsize()
        mem = self.read_mem(address, intsize)
        if mem:
            value = self.unpack(mem, intsize)
            return value
        else:
            return None

    def writemem(self, address, buf):
        """
        Write buf to memory start at an address

        Args:
            - address: start address to write (Int)
            - buf: data to write (raw bytes)

        Returns:
            - number of written bytes (Int)
        """
        out = None
        if not buf:
            return 0

        if self.getpid():
            # try fast restore mem
            tmp = utils.tmpfile(is_binary_file=True)
            tmp.write(buf)
            tmp.flush()
            out = self.execute("restore %s binary %#x" % (tmp.name, address), to_string=True)
            tmp.close()
        if not out:  # try the slow way
            for i in range(len(buf)):
                if not self.execute("set {char}%#x = %#x" % (address + i, ord(buf[i]))):
                    return i
            return i + 1
        elif "error" in out:  # failed to write the whole buf, find written byte
            for i in range(0, len(buf), 1):
                if not self.is_address(address + i):
                    return i
        else:
            return len(buf)

    def write_int(self, address, value, intsize=None):
        """
        Write an interger value to memory

        Args:
            - address: address to read (Int)
            - value: int to write to (Int)
            - intsize: force write size (Int)

        Returns:
            - Bool
        """
        if not intsize:
            intsize = self.intsize()
        buf = hex2str(value, intsize).ljust(intsize, "\x00")[:intsize]
        saved = self.read_mem(address, intsize)
        if not saved:
            return False

        ret = self.writemem(address, buf)
        if ret != intsize:
            self.writemem(address, saved)
            return False
        return True

    def write_long(self, address, value):
        """
        Write a long long value to memory

        Args:
            - address: address to read (Int)
            - value: value to write to

        Returns:
            - Bool
        """
        return self.write_int(address, value, 8)

    def cmpmem(self, start, end, buf):
        """
        Compare contents of a memory region with a buffer

        Args:
            - start: start address (Int)
            - end: end address (Int)
            - buf: raw bytes

        Returns:
            - dictionary of array of diffed bytes in hex (Dictionary)
            {123: [("A", "B"), ("C", "C"))]}
        """
        line_len = 32
        if end < start:
            (start, end) = (end, start)

        mem = self.dumpmem(start, end)
        if mem is None:
            return None

        length = min(len(mem), len(buf))
        result = {}
        lineno = 0
        for i in range(length // line_len):
            diff = 0
            bytes_ = []
            for j in range(line_len):
                offset = i * line_len + j
                bytes_ += [(mem[offset:offset + 1], buf[offset:offset + 1])]
                if mem[offset] != buf[offset]:
                    diff = 1
            if diff == 1:
                result[start + lineno] = bytes_
            lineno += line_len

        bytes_ = []
        diff = 0
        for i in range(length % line_len):
            offset = lineno + i
            bytes_ += [(mem[offset:offset + 1], buf[offset:offset + 1])]
            if mem[offset] != buf[offset]:
                diff = 1
        if diff == 1:
            result[start + lineno] = bytes_

        return result

    def xormem(self, start, end, key):
        """
        XOR a memory region with a key

        Args:
            - start: start address (Int)
            - end: end address (Int)
            - key: XOR key (String)

        Returns:
            - xored memory content (raw bytes)
        """
        mem = self.dumpmem(start, end)
        if mem is None:
            return None

        if to_int(key) is not None:
            key = hex2str(to_int(key), self.intsize())
        mem = list(utils.bytes_iterator(mem))
        for index, char in enumerate(mem):
            key_idx = index % len(key)
            mem[index] = chr(ord(char) ^ ord(key[key_idx]))

        buf = b"".join([utils.to_binary_string(x) for x in mem])
        bytes = self.writemem(start, buf)
        return buf

    def searchmem(self, start, end, search, mem=None):
        """
        Search for all instances of a pattern in memory from start to end

        Args:
            - start: start address (Int)
            - end: end address (Int)
            - search: string or python regex pattern (String)
            - mem: cached mem to not re-read for repeated searches (raw bytes)

        Returns:
            - list of found result: (address(Int), hex encoded value(String))
        """
        result = []
        if end < start:
            (start, end) = (end, start)

        if mem is None:
            mem = self.dumpmem(start, end)

        if not mem:
            return result

        if isinstance(search, six.string_types) and search.startswith("0x"):
            # hex number
            search = search[2:]
            if len(search) % 2 != 0:
                search = "0" + search
            search = codecs.decode(search, 'hex')[::-1]
            search = re.escape(search)

        # Convert search to bytes if is not already
        if not isinstance(search, bytes):
            search = search.encode('utf-8')

        try:
            p = re.compile(search)
        except:
            search = re.escape(search)
            p = re.compile(search)

        for m in p.finditer(mem):
            index = 1
            if m.start() == m.end() and m.lastindex:
                index = m.lastindex + 1
            for i in range(0, index):
                if m.start(i) != m.end(i):
                    result += [(start + m.start(i), codecs.encode(mem[m.start(i):m.end(i)], 'hex'))]

        return result

    def searchmem_by_range(self, mapname, search):
        """
        Search for all instances of a pattern in virtual memory ranges

        Args:
            - search: string or python regex pattern (String)
            - mapname: name of virtual memory range (String)

        Returns:
            - list of found result: (address(Int), hex encoded value(String))
        """
        result = []
        ranges = self.get_vmmap(mapname)
        if ranges:
            for (start, end, perm, name) in ranges:
                if "r" in perm:
                    result += self.searchmem(start, end, search)

        return result

    @memoized
    def search_reference(self, search, mapname=None):
        """
        Search for all references to a value in memory ranges

        Args:
            - search: string or python regex pattern (String)
            - mapname: name of target virtual memory range (String)

        Returns:
            - list of found result: (address(int), hex encoded value(String))
        """
        maps = self.get_vmmap()
        ranges = self.get_vmmap(mapname)
        result = []
        search_result = []
        for (start, end, perm, name) in maps:
            if "r" in perm:
                search_result += self.searchmem(start, end, search)

        for (start, end, perm, name) in ranges:
            for (a, v) in search_result:
                result += self.searchmem(start, end, to_address(a))

        return result

    def search_address(self, searchfor, belongto):
        """
        Search for all valid addresses in memory ranges

        Args:
            - searchfor: memory region to search for addresses (String or Tuple)
            - belongto: memory region that target addresses belong to (String)

        Returns:
            - list of found result: (address(Int), value(Int))
        """
        result = []
        maps = self.get_vmmap()
        if maps is None:
            return result

        if isinstance(searchfor, str):
            searchfor_ranges = self.get_vmmap(searchfor)
        else:
            searchfor_ranges = [(*searchfor, None, None)]
        belongto_ranges = self.get_vmmap(belongto)
        step = self.intsize()
        for (start, end, _, _) in searchfor_ranges[::-1]:  # dirty trick, to search in rw-p mem first
            mem = self.dumpmem(start, end)
            if not mem:
                continue
            for i in range(0, len(mem) - step + 1, step):  # abandon unaligned bytes
                addr = self.unpack(mem[i:i + step], step)
                if self.is_address(addr, belongto_ranges):
                    result += [(start + i, addr)]

        return result

    def search_pointer(self, searchfor, belongto):
        """
        Search for all valid pointers in memory ranges

        Args:
            - searchfor: memory region to search for pointers (String or Tuple)
            - belongto: memory region that pointed addresses belong to (String)

        Returns:
            - list of found result: (address(Int), value(Int))
        """
        search_result = []
        result = []
        maps = self.get_vmmap()
        if isinstance(searchfor, str):
            searchfor_ranges = self.get_vmmap(searchfor)
        else:
            searchfor_ranges = [(*searchfor, None, None)]
        belongto_ranges = self.get_vmmap(belongto)
        step = self.intsize()
        for (start, end, _, _) in searchfor_ranges[::-1]:
            mem = self.dumpmem(start, end)
            if not mem:
                continue
            for i in range(0, len(mem) - step + 1, step):
                addr = self.unpack(mem[i:i + step], step)
                if self.is_address(addr):
                    (v, t, vn) = self.examine_mem_value(addr)
                    if t != 'value':
                        if self.is_address(to_int(vn), belongto_ranges):
                            if (to_int(v), v) not in search_result:
                                search_result += [(to_int(v), v)]

            for (a, v) in search_result:
                result += self.searchmem(start, end, to_address(a), mem)

        return result

    @memoized
    def examine_mem_value(self, value):
        """
        Examine a value in memory for its type and reference

        Args:
            - value: value to examine (Int)

        Returns:
            - tuple of (value(Int), type(String), next_value(Int))
        """

        def examine_data(value):
            intsize = self.intsize()
            out = self.read_int(value, intsize)
            if out is not None and utils.is_printable(int2hexstr(out, intsize)):
                out = self.execute("x/s %#x" % value, to_string=True).split(":", 1)[1].strip()
            return out

        result = (None, None, None)
        if value is None:
            return result

        maps = self.get_vmmap()
        binmap = self.get_vmmap("binary")

        if not self.is_address(value):  # a value
            result = (to_hex(value), "value", "")
            return result
        else:
            (_, _, _, mapname) = self.get_vmrange(value)

        # check for writable first so rwxp mem will be treated as data
        if self.is_writable(value):  # writable data address
            out = examine_data(value)
            if out is not None:
                heapmap = self.get_vmmap("heap")
                if heapmap:
                    (heap_start, heap_end, perm, mapname) = heapmap[0]
                    if value >= heap_start and value < heap_end:
                        result = (to_hex(value), "heap", out)
                    else:
                        result = (to_hex(value), "data", out)
                else:
                    result = (to_hex(value), "data", out)

        elif self.is_executable(value):  # code/rodata address
            if self.is_address(value, binmap):
                headers = self.elfheader()
            else:
                headers = self.elfheader_solib(mapname)

            if headers:
                headers = sorted(headers.items(), key=lambda x: x[1][1])
                for (k, (start, end, type)) in headers:
                    if value >= start and value < end:
                        if type == "code":
                            out = self.get_disasm(value)
                            m = re.search(".*?0x\S+?\s(.*)", out)
                            result = (to_hex(value), "code", m.group(1))
                        else:  # rodata address
                            result = (to_hex(value), "rodata", examine_data(value))
                        break

                if result[0] is None:  # not fall to any header section
                    result = (to_hex(value), "rodata", examine_data(value))

            else:  # not belong to any lib: [heap], [vdso], [vsyscall], etc
                out = self.get_disasm(value)
                if "(bad)" in out:
                    result = (to_hex(value), "rodata", examine_data(value))
                else:
                    m = re.search(".*?0x\S+?\s(.*)", out)
                    result = (to_hex(value), "code", m.group(1))

        else:  # readonly data address
            out = examine_data(value)
            if out is not None:
                result = (to_hex(value), "rodata", out)
            else:
                result = (to_hex(value), "rodata", "MemError")

        return result

    @memoized
    def examine_mem_reference(self, value, depth=5):
        """
        Deeply examine a value in memory for its references

        Args:
            - value: value to examine (Int)

        Returns:
            - list of tuple of (value(Int), type(String), next_value(Int))
        """
        result = []
        if depth <= 0:
            depth = 0xffffffff

        (v, t, vn) = self.examine_mem_value(value)
        while vn is not None:
            if len(result) > depth:
                _v, _t, _vn = result[-1]
                result[-1] = (_v, _t, "--> ...")
                break

            result += [(v, t, vn)]
            v_int = to_int(v)
            vn_int = to_int(vn)
            if v == vn or v_int == vn_int:  # point to self
                break
            if vn_int is None:
                break
            if vn_int in [to_int(v) for (v, _, _) in result]:  # point back to previous value
                break
            (v, t, vn) = self.examine_mem_value(to_int(vn))

        return result

    @memoized
    def format_search_result(self, result, display=256):
        """
        Format the result from various memory search commands

        Args:
            - result: result of search commands (List)
            - display: number of items to display

        Returns:
            - text: formatted text (String)
        """
        text = ""
        if not result:
            text = "Not found"
        else:
            maxlen = 0
            maps = self.get_vmmap()
            shortmaps = []
            for (start, end, perm, name) in maps:
                shortname = os.path.basename(name)
                if shortname.startswith("lib"):
                    shortname = shortname.split("-")[0]
                shortmaps += [(start, end, perm, shortname)]

            count = len(result)
            if display != 0:
                count = min(count, display)
            text += "Found %d results, display max %d items:\n" % (len(result), count)
            for (addr, v) in result[:count]:
                vmrange = self.get_vmrange(addr, shortmaps)
                maxlen = max(maxlen, len(vmrange[3]))

            for (addr, v) in result[:count]:
                vmrange = self.get_vmrange(addr, shortmaps)
                chain = self.examine_mem_reference(addr)
                text += "%s : %s" % (vmrange[3].rjust(maxlen), format_reference_chain(chain) + "\n")

        return text

    ##########################
    #     Exploit Helpers    #
    ##########################
    @memoized
    def elfentry(self):
        """
        Get entry point address of debugged ELF file

        Returns:
            - entry address (Int)
        """
        out = self.execute("info files", to_string=True)
        if out:
            m = re.search("Entry point: ([^\s]*)", out)
            if m:
                return to_int(m.group(1))
        return None

    @memoized
    def elfheader(self, name=None):
        """
        Get headers information of debugged ELF file

        Args:
            - name: specific header name (String)

        Returns:
            - dictionary of headers {name(String): (start(Int), end(Int), type(String))}
        """
        elfinfo = {}
        elfbase = 0
        if self.getpid():
            binmap = self.get_vmmap("binary")
            elfbase = binmap[0][0] if binmap else 0

        out = self.execute("maintenance info sections", to_string=True)
        if not out:
            return {}

        matches = re.findall("\s*(0x[^-]*)->(0x\S+) at (0x[^:]*):\s*([^ ]*)\s*(.*)", out)

        for (start, end, offset, hname, attr) in matches:
            start, end, offset = to_int(start), to_int(end), to_int(offset)
            # skip unuseful header
            if start < offset:
                continue
            # if PIE binary, update with runtime address
            if start < elfbase:
                start += elfbase
                end += elfbase

            if "CODE" in attr:
                htype = "code"
            elif "READONLY" in attr:
                htype = "rodata"
            else:
                htype = "data"

            elfinfo[hname.strip()] = (start, end, htype)

        result = {}
        if name is None:
            result = elfinfo
        else:
            if name in elfinfo:
                result[name] = elfinfo[name]
            else:
                for (k, v) in elfinfo.items():
                    if name in k:
                        result[k] = v
        return result

    @memoized
    def elfsymbols(self, pattern=None):
        """
        Get all non-debugging symbol information of debugged ELF file

        Returns:
            - dictionary of (address(Int), symname(String))
        """
        headers = self.elfheader()
        if ".plt" not in headers:  # static binary
            return {}

        binmap = self.get_vmmap("binary")
        elfbase = binmap[0][0] if binmap else 0

        # get the .dynstr header
        headers = self.elfheader()
        if ".dynstr" not in headers:
            return {}
        (start, end, _) = headers[".dynstr"]
        mem = self.dumpmem(start, end)
        if not mem and self.getfile():
            fd = open(self.getfile())
            fd.seek(start, 0)
            mem = fd.read(end - start)
            fd.close()

        # Convert names into strings
        dynstrings = [name.decode('utf-8') for name in mem.split(b"\x00")]

        if pattern:
            dynstrings = [s for s in dynstrings if re.search(pattern, s)]

        # get symname@plt info
        symbols = {}
        for symname in dynstrings:
            if not symname: continue
            symname += "@plt"
            out = self.execute("info functions %s" % symname, to_string=True)
            if not out: continue
            m = re.findall(".*(0x\S+)\s*%s" % re.escape(symname), out)
            for addr in m:
                addr = to_int(addr)
                if self.is_address(addr, binmap):
                    if symname not in symbols:
                        symbols[symname] = addr
                        break

        # if PIE binary, update with runtime address
        for (k, v) in symbols.items():
            if v < elfbase:
                symbols[k] = v + elfbase

        return symbols

    @memoized
    def elfsymbol(self, symname=None):
        """
        Get non-debugging symbol information of debugged ELF file

        Args:
            - name: target function name (String), special cases:
                + "data": data transfer functions
                + "exec": exec helper functions

        Returns:
            - if exact name is not provided: dictionary of tuple (symname, plt_entry)
            - if exact name is provided: dictionary of tuple (symname, plt_entry, got_entry, reloc_entry)
        """
        datafuncs = ["printf", "puts", "gets", "cpy"]
        execfuncs = ["system", "exec", "mprotect", "mmap", "syscall"]
        result = {}
        if not symname or symname in ["data", "exec"]:
            symbols = self.elfsymbols()
        else:
            symbols = self.elfsymbols(symname)

        if not symname:
            result = symbols
        else:
            sname = symname.replace("@plt", "") + "@plt"
            if sname in symbols:
                plt_addr = symbols[sname]
                result[sname] = plt_addr  # plt entry
                out = self.get_disasm(plt_addr, 2)
                for line in out.splitlines():
                    if "jmp" in line:
                        addr = to_int("0x" + line.strip().rsplit("0x")[-1].split()[0])
                        result[sname.replace("@plt", "@got")] = addr  # got entry
                    if "push" in line:
                        addr = to_int("0x" + line.strip().rsplit("0x")[-1])
                        result[sname.replace("@plt", "@reloc")] = addr  # reloc offset
            else:
                keywords = [symname]
                if symname == "data":
                    keywords = datafuncs
                if symname == "exec":
                    keywords = execfuncs
                for (k, v) in symbols.items():
                    for f in keywords:
                        if f in k:
                            result[k] = v

        return result

    @memoized
    def main_entry(self):
        """
        Get address of main function of stripped ELF file

        Returns:
            - main function address (Int)
        """
        refs = self.xrefs("__libc_start_main@plt")
        if refs:
            inst = self.prev_inst(refs[0][0])
            if inst:
                addr = re.search(".*(0x.*)", inst[0][1])
                if addr:
                    return to_int(addr.group(1))
        return None

    @memoized
    def readelf_header(self, filename, name=None):
        """
        Get headers information of an ELF file using 'readelf'

        Args:
            - filename: ELF file (String)
            - name: specific header name (String)

        Returns:
            - dictionary of headers (name(String), value(Int)) (Dict)
        """
        elfinfo = {}
        result = {}
        vmap = self.get_vmmap(filename)
        elfbase = vmap[0][0] if vmap else 0
        out = utils.execute_external_command("%s -W -S %s" % (config.READELF, filename))
        if not out:
            return {}

        matches = re.findall(".*\[.*\] (\.[^ ]*) [^0-9]* ([^ ]*) [^ ]* ([^ ]*)(.*)", out)
        if not matches:
            return result

        for (hname, start, size, attr) in matches:
            start, end = to_int("0x" + start), to_int("0x" + start) + to_int("0x" + size)
            # if PIE binary or DSO, update with runtime address
            if start < elfbase:
                start += elfbase
            if end < elfbase:
                end += elfbase

            if "X" in attr:
                htype = "code"
            elif "W" in attr:
                htype = "data"
            else:
                htype = "rodata"
            elfinfo[hname.strip()] = (start, end, htype)

        if name is None:
            result = elfinfo
        else:
            if name in elfinfo:
                result[name] = elfinfo[name]
            else:
                for (k, v) in elfinfo.items():
                    if name in k:
                        result[k] = v
        return result

    @memoized
    def elfheader_solib(self, solib=None, name=None):
        """
        Get headers information of Shared Object Libraries linked to target

        Args:
            - solib: shared library name (String)
            - name: specific header name (String)

        Returns:
            - dictionary of headers {name(String): start(Int), end(Int), type(String))
        """
        # hardcoded ELF header type
        header_type = {
            "code": [".text", ".fini", ".init", ".plt", "__libc_freeres_fn"],
            "data": [
                ".dynamic", ".data", ".ctors", ".dtors", ".jrc", ".got", ".got.plt", ".bss", ".tdata", ".tbss",
                ".data.rel.ro", ".fini_array", "__libc_subfreeres", "__libc_thread_subfreeres"
            ]
        }

        @memoized
        def _elfheader_solib_all():
            out = self.execute("info files", to_string=True)
            if not out:
                return None

            soheaders = re.findall("[^\n]*\s*(0x\S+) - (0x\S+) is (\.[^ ]*) in (.*)", out)

            result = []
            for (start, end, hname, libname) in soheaders:
                start, end = to_int(start), to_int(end)
                result += [(start, end, hname, os.path.realpath(libname))
                           ]  # tricky, return the realpath version of libraries
            return result

        elfinfo = {}

        headers = _elfheader_solib_all()
        if not headers:
            return {}

        if solib is None:
            return headers

        vmap = self.get_vmmap(solib)
        elfbase = vmap[0][0] if vmap else 0

        for (start, end, hname, libname) in headers:
            if solib in libname:
                # if PIE binary or DSO, update with runtime address
                if start < elfbase:
                    start += elfbase
                if end < elfbase:
                    end += elfbase
                # determine the type
                htype = "rodata"
                if hname in header_type["code"]:
                    htype = "code"
                elif hname in header_type["data"]:
                    htype = "data"
                elfinfo[hname.strip()] = (start, end, htype)

        result = {}
        if name is None:
            result = elfinfo
        else:
            if name in elfinfo:
                result[name] = elfinfo[name]
            else:
                for (k, v) in elfinfo.items():
                    if name in k:
                        result[k] = v
        return result

    def checksec(self, filename=None):
        """
        Check for various security options of binary (ref: http://www.trapkit.de/tools/checksec.sh)

        Args:
            - file: path name of file to check (String)

        Returns:
            - dictionary of (setting(String), status(Int)) (Dict)
        """
        result = {}
        result["RELRO"] = 0
        result["CANARY"] = 0
        result["NX"] = 1
        result["PIE"] = 0
        result["FORTIFY"] = 0

        if filename is None:
            filename = self.getfile()

        if not filename:
            return None

        out = utils.execute_external_command("%s -W -a \"%s\" 2>&1" % (config.READELF, filename))
        if "Error:" in out:
            return None

        for line in out.splitlines():
            if "GNU_RELRO" in line:
                result["RELRO"] |= 2
            if "BIND_NOW" in line:
                result["RELRO"] |= 1
            if "__stack_chk_fail" in line:
                result["CANARY"] = 1
            if "GNU_STACK" in line and "RWE" in line:
                result["NX"] = 0
            if "Type:" in line and "DYN (" in line:
                result["PIE"] = 4  # Dynamic Shared Object
            if "(DEBUG)" in line and result["PIE"] == 4:
                result["PIE"] = 1
            if "_chk@" in line:
                result["FORTIFY"] = 1

        if result["RELRO"] == 1:
            result["RELRO"] = 0  # ? | BIND_NOW + NO GNU_RELRO = NO PROTECTION
        # result["RELRO"] == 2 # Partial | NO BIND_NOW + GNU_RELRO
        # result["RELRO"] == 3 # Full | BIND_NOW + GNU_RELRO
        return result

    @memoized
    def search_asm(self, start, end, asmcode):
        """
        Search for ASM instructions in memory

        Args:
            - start: start address (Int)
            - end: end address (Int)
            - asmcode: assembly instruction (String)
                + multiple instructions are separated by ";"
                + wildcard ? supported, will be replaced by registers or multi-bytes

        Returns:
            - list of (address(Int), hexbyte(String))
        """
        wildcard = asmcode.count('?')
        magic_bytes = ["0x00", "0xff", "0xdead", "0xdeadbeef", "0xdeadbeefdeadbeef"]

        ops = [x for x in asmcode.split(';') if x]

        def buildcode(code=b"", pos=0, depth=0):
            if depth == wildcard and pos == len(ops):
                yield code
                return

            c = ops[pos].count('?')
            if c > 2: return
            elif c == 0:
                asm = self.assemble(ops[pos])
                if asm:
                    for code in buildcode(code + asm, pos + 1, depth):
                        yield code
            else:
                save = ops[pos]
                for regs in REGISTERS.values():
                    for reg in regs:
                        ops[pos] = save.replace("?", reg, 1)
                        for asmcode_reg in buildcode(code, pos, depth + 1):
                            yield asmcode_reg
                for byte in magic_bytes:
                    ops[pos] = save.replace("?", byte, 1)
                    for asmcode_mem in buildcode(code, pos, depth + 1):
                        yield asmcode_mem
                ops[pos] = save

        searches = []

        def decode_hex_escape(str_):
            """Decode string as hex and escape for regex"""
            return re.escape(codecs.decode(str_, 'hex'))

        for machine_code in buildcode():
            search = re.escape(machine_code)
            search = search.replace(decode_hex_escape(b"dead"), b"..")\
                .replace(decode_hex_escape(b"beef"), b"..")\
                .replace(decode_hex_escape(b"00"), b".")\
                .replace(decode_hex_escape(b"ff"), b".")

            searches.append(search)

        if not searches:
            warning_msg("invalid asmcode: '%s'" % asmcode)
            return []

        search = b"(?=(" + b"|".join(searches) + b"))"
        candidates = self.searchmem(start, end, search)

        result = []
        for (a, v) in candidates:
            asmcode = self.execute("disassemble %#x, %#x" % (a, a + (len(v) // 2)), to_string=True)
            if asmcode:
                asmcode = "\n".join(asmcode.splitlines()[1:-1])
                matches = re.findall(".*:([^\n]*)", asmcode)
                result += [(a, (v, ";".join(matches).strip()))]

        return result

    def search_jmpcall(self, start, end, regname=None):
        """
        Search memory for jmp/call reg instructions

        Args:
            - start: start address (Int)
            - end: end address (Int)
            - reg: register name (String)

        Returns:
            - list of (address(Int), instruction(String))
        """
        result = []
        REG = {0: "eax", 1: "ecx", 2: "edx", 3: "ebx", 4: "esp", 5: "ebp", 6: "esi", 7: "edi"}
        P2REG = {0: "[eax]", 1: "[ecx]", 2: "[edx]", 3: "[ebx]", 6: "[esi]", 7: "[edi]"}
        OPCODE = {0xe: "jmp", 0xd: "call"}
        P2OPCODE = {0x1: "call", 0x2: "jmp"}
        JMPREG = [b"\xff" + utils.bytes_chr(i) for i in range(0xe0, 0xe8)]
        JMPREG += [b"\xff" + utils.bytes_chr(i) for i in range(0x20, 0x28)]
        CALLREG = [b"\xff" + utils.bytes_chr(i) for i in range(0xd0, 0xd8)]
        CALLREG += [b"\xff" + utils.bytes_chr(i) for i in range(0x10, 0x18)]
        JMPCALL = JMPREG + CALLREG

        if regname is None:
            regname = ""
        regname = regname.lower()
        mem = self.dumpmem(start, end)
        found = re.finditer(b'|'.join(JMPCALL).replace(b' ', b'\ '), mem)
        (arch, bits) = self.getarch()
        for m in list(found):
            inst = ""
            addr = start + m.start()
            opcode = codecs.encode(m.group()[1:2], 'hex')
            type = int(opcode[0:1], 16)
            reg = int(opcode[1:2], 16)
            if type in OPCODE:
                inst = OPCODE[type] + " " + REG[reg]

            if type in P2OPCODE and reg in P2REG:
                inst = P2OPCODE[type] + " " + P2REG[reg]

            if inst != "" and regname[-2:] in inst.split()[-1]:
                if bits == 64:
                    inst = inst.replace("e", "r")
                result += [(addr, inst)]

        return result

    def search_substr(self, start, end, search, mem=None):
        """
        Search for substrings of a given string/number in memory

        Args:
            - start: start address (Int)
            - end: end address (Int)
            - search: string to search for (String)
            - mem: cached memory (raw bytes)

        Returns:
            - list of tuple (substr(String), address(Int))
        """

        def substr(s1, s2):
            "Search for a string in another string"
            s1 = utils.to_binary_string(s1)
            s2 = utils.to_binary_string(s2)
            i = 1
            found = 0
            while i <= len(s1):
                if s2.find(s1[:i]) != -1:
                    found = 1
                    i += 1
                    if s1[:i - 1][-1:] == b"\x00":
                        break
                else:
                    break
            if found == 1:
                return i - 1
            else:
                return -1

        result = []
        if end < start:
            start, end = end, start

        if mem is None:
            mem = self.dumpmem(start, end)

        if search[:2] == "0x":  # hex number
            search = search[2:]
            if len(search) % 2 != 0:
                search = "0" + search
            search = codecs.decode(search, 'hex')[::-1]
        search = utils.to_binary_string(utils.decode_string_escape(search))
        while search:
            l = len(search)
            i = substr(search, mem)
            if i != -1:
                sub = search[:i]
                addr = start + mem.find(sub)
                if not utils.check_badchars(addr):
                    result.append((sub, addr))
            else:
                result.append((search, -1))
                return result
            search = search[i:]
        return result


###########################################################################
class PEDACmd(object):
    """
    Class for PEDA commands that interact with GDB
    """
    commands = []

    def __init__(self):
        # list of all available commands
        self.commands = [c for c in dir(self) if callable(getattr(self, c)) and not c.startswith("_")]
        self.mode = 0
        self._diff_regs = {}

    ##################
    #   Misc Utils   #
    ##################
    def _missing_argument(self):
        """
        Raise exception for missing argument, for internal use
        """
        text = "missing argument"
        error_msg(text)
        raise Exception(text)

    def _is_running(self):
        """
        Check if program is running, for internal use
        """
        pid = peda.getpid()
        if pid is None:
            text = "not running"
            warning_msg(text)
            return None
            #raise Exception(text)
        else:
            return pid

    def enable(self):
        """
        Enable peda display
        """
        if not peda.enabled:
            peda.restore_user_command("all")
            peda.enabled = True

    def disable(self):
        """
        Disable peda display
        """
        if peda.enabled:
            peda.save_user_command("hook-stop")
            peda.enabled = False

    def reload(self, *arg):
        """
        Reload PEDA sources, keep current options untouch
        Usage:
            MYNAME [name]
        """
        (modname, ) = normalize_argv(arg, 1)
        # save current PEDA options
        saved_opt = config.Option
        peda_path = os.path.dirname(PEDAFILE) + "/lib/"
        if not modname:
            modname = "PEDA"  # just for notification
            ret = peda.execute("source %s" % PEDAFILE)
        else:
            if not modname.endswith(".py"):
                modname = modname + ".py"
            filepath = "%s/%s" % (peda_path, modname)
            if os.path.exists(filepath):
                ret = peda.execute("source %s" % filepath)
                peda.execute("source %s" % PEDAFILE)
            else:
                ret = False

        config.Option = saved_opt
        if ret:
            msg("%s reloaded!" % modname, "blue")
        else:
            msg("Failed to reload %s source from: %s" % (modname, peda_path))

    def _get_helptext(self, *arg):
        """
        Get the help text, for internal use by help command and other aliases
        """
        (cmd, ) = normalize_argv(arg, 1)
        helptext = ""
        if cmd is None:
            helptext = red("PEDA", "bold") + blue(" - Python Exploit Development Assistance for GDB", "bold") + "\n"
            helptext += "For latest update, check peda project page: %s\n" % green("https://github.com/longld/peda/")
            helptext += "List of \"peda\" subcommands, type the subcommand to invoke it:\n"
            i = 0
            for cmd in self.commands:
                if cmd.startswith("_"): continue  # skip internal use commands
                func = getattr(self, cmd)
                helptext += "%s -- %s\n" % (cmd, green(utils.trim(func.__doc__.strip("\n").splitlines()[0])))
            helptext += "\nType \"help\" followed by subcommand for full documentation."
        else:
            if cmd in self.commands:
                func = getattr(self, cmd)
                lines = utils.trim(func.__doc__).splitlines()
                helptext += green(lines[0]) + "\n"
                for line in lines[1:]:
                    if "Usage:" in line:
                        helptext += blue(line) + "\n"
                    else:
                        helptext += line + "\n"
            else:
                for c in self.commands:
                    if not c.startswith("_") and cmd in c:
                        func = getattr(self, c)
                        helptext += "%s -- %s\n" % (c, green(utils.trim(func.__doc__.strip("\n").splitlines()[0])))

        return helptext

    def help(self, *arg):
        """
        Print the usage manual for PEDA commands
        Usage:
            MYNAME
            MYNAME command
        """
        msg(self._get_helptext(*arg))

    help.options = commands

    def pyhelp(self, *arg):
        """
        Wrapper for python built-in help
        Usage:
            MYNAME (enter interactive help)
            MYNAME help_request
        """
        (request, ) = normalize_argv(arg, 1)
        if request is None:
            help()
            return

        peda_methods = ["%s" % c for c in dir(PEDA) if callable(getattr(PEDA, c)) and \
                                not c.startswith("_")]

        if request in peda_methods:
            request = "peda.%s" % request
        try:
            if request.lower().startswith("peda"):
                request = eval(request)
                help(request)
                return

            if "." in request:
                module, _, function = request.rpartition('.')
                if module:
                    module = module.split(".")[0]
                    __import__(module)
                    mod = sys.modules[module]
                    if function:
                        request = getattr(mod, function)
                    else:
                        request = mod
            else:
                mod = sys.modules['__main__']
                request = getattr(mod, request)

            # wrapper for python built-in help
            help(request)
        except:  # fallback to built-in help
            try:
                help(request)
            except Exception as e:
                if config.Option.get("debug") == "on":
                    msg('Exception (%s): %s' % ('pyhelp', e), "red")
                    traceback.print_exc()
                msg("no Python documentation found for '%s'" % request)

    pyhelp.options = ["%s" % c for c in dir(PEDA) if callable(getattr(PEDA, c)) and \
                        not c.startswith("_")]

    # show [option | args | env]
    def show(self, *arg):
        """
        Show various PEDA options and other settings
        Usage:
            MYNAME option [optname]
            MYNAME (show all options)
            MYNAME args
            MYNAME env [envname]
        """
        # show options
        def _show_option(name=None):
            if name is None:
                name = ""
            filename = peda.getfile()
            if filename:
                filename = os.path.basename(filename)
            else:
                filename = None
            for (k, v) in sorted(config.Option.show(name).items()):
                if filename and isinstance(v, str) and "#FILENAME#" in v:
                    v = v.replace("#FILENAME#", filename)
                msg("%s = %s" % (k, repr(v)))

        # show args
        def _show_arg():
            arg = peda.execute("show args", to_string=True)
            arg = arg.split("started is ")[1][1:-3]
            arg = (peda.string_to_argv(arg))
            if not arg:
                msg("No argument")
            for i, a in enumerate(arg):
                text = "arg[%d]: %s" % ((i + 1), a if utils.is_printable(a) else to_hexstr(a))
                msg(text)

        # show envs
        def _show_env(name=None):
            if name is None:
                name = ""
            env = peda.execute("show env", to_string=True)
            for line in env.splitlines():
                (k, v) = line.split("=", 1)
                if k.startswith(name):
                    msg("%s = %s" % (k, v if utils.is_printable(v) else to_hexstr(v)))

        (opt, name) = normalize_argv(arg, 2)

        if opt is None or opt.startswith("opt"):
            _show_option(name)
        elif opt.startswith("arg"):
            _show_arg()
        elif opt.startswith("env"):
            _show_env(name)
        else:
            msg("Unknown show option: %s" % opt)

    show.options = ["option", "arg", "env"]

    # set [option | arg | env]
    def set(self, *arg):
        """
        Set various PEDA options and other settings
        Usage:
            MYNAME option name value
            MYNAME arg string
            MYNAME env name value
                support input non-printable chars, e.g MYNAME env EGG "\\x90"*1000
        """
        # set options
        def _set_option(name, value):
            if name in config.Option.options:
                config.Option.set(name, value)
                msg("%s = %s" % (name, repr(value)))
            else:
                msg("Unknown option: %s" % name)

        # set args
        def _set_arg(*arg):
            cmd = "set args"
            for a in arg:
                try:
                    s = eval('%s' % a)
                    if isinstance(s, six.integer_types + six.string_types):
                        a = s
                except:
                    pass
                cmd += " '%s'" % a
            peda.execute(cmd)

        # set env
        def _set_env(name, value):
            env = peda.execute("show env", to_string=True)
            cmd = "set env %s " % name
            try:
                value = eval('%s' % value)
            except:
                pass
            cmd += '%s' % value
            peda.execute(cmd)

        (opt, name, value) = normalize_argv(arg, 3)
        if opt is None:
            self._missing_argument()

        if opt.startswith("opt"):
            if value is None:
                self._missing_argument()
            _set_option(name, value)
        elif opt.startswith("arg"):
            _set_arg(*arg[1:])
        elif opt.startswith("env"):
            _set_env(name, value)
        else:
            msg("Unknown set option: %s" % known_args.opt)

    set.options = ["option", "arg", "env"]

    def hexprint(self, *arg):
        """
        Display hexified of data in memory
        Usage:
            MYNAME address (display 16 bytes from address)
            MYNAME address count
            MYNAME address /count (display "count" lines, 16-bytes each)
        """
        (address, count) = normalize_argv(arg, 2)
        if address is None:
            self._missing_argument()

        if count is None:
            count = 16

        if not to_int(count) and count.startswith("/"):
            count = to_int(count[1:])
            count = count * 16 if count else None

        bytes_ = peda.dumpmem(address, address + count)
        if bytes_ is None:
            warning_msg("cannot retrieve memory content")
        else:
            hexstr = to_hexstr(bytes_)
            linelen = 16  # display 16-bytes per line
            i = 0
            text = ""
            while hexstr:
                text += '%s : "%s"\n' % (blue(to_address(address + i * linelen)), hexstr[:linelen * 4])
                hexstr = hexstr[linelen * 4:]
                i += 1
            pager(text)

    def hexdump(self, *arg):
        """
        Display hex/ascii dump of data in memory
        Usage:
            MYNAME address (dump 16 bytes from address)
            MYNAME address count
            MYNAME address /count (dump "count" lines, 16-bytes each)
        """

        def ascii_char(ch):
            if ord(ch) >= 0x20 and ord(ch) < 0x7e:
                return chr(ord(ch))  # Ensure we return a str
            else:
                return "."

        (address, count) = normalize_argv(arg, 2)
        if address is None:
            self._missing_argument()

        if count is None:
            count = 0x40

        if not to_int(count) and count.startswith("/"):
            count = to_int(count[1:])
            count = count * 16 if count else None

        bytes_ = peda.dumpmem(address, address + count)
        if bytes_ is None:
            warning_msg("cannot retrieve memory content")
        else:
            linelen = 16  # display 16-bytes per line
            i = 0
            text = ""
            while bytes_:
                buf = bytes_[:linelen]
                hexbytes = " ".join(["%02x" % ord(c) for c in utils.bytes_iterator(buf)])
                asciibytes = "".join([ascii_char(c) for c in utils.bytes_iterator(buf)])
                text += '%s : %s  %s\n' % (blue(to_address(address + i * linelen)), hexbytes.ljust(
                    linelen * 3), asciibytes)
                bytes_ = bytes_[linelen:]
                i += 1
            pager(text)

    def aslr(self, *arg):
        """
        Show/set ASLR setting of GDB
        Usage:
            MYNAME [on|off]
        """
        (option, ) = normalize_argv(arg, 1)
        if option is None:
            out = peda.execute("show disable-randomization", to_string=True)
            if not out:
                warning_msg("ASLR setting is unknown or not available")
                return

            if "is off" in out:
                msg("ASLR is %s" % green("ON"))
            elif "is on" in out:
                msg("ASLR is %s" % red("OFF"))
        else:
            option = option.strip().lower()
            if option in ["on", "off"]:
                peda.execute("set disable-randomization %s" % ("off" if option == "on" else "on"))

    def distance(self, *arg):
        """
        Calculate distance between two addresses
        Usage:
            MYNAME address (calculate from current $SP to address)
            MYNAME address1 address2
        """
        (end, start) = normalize_argv(arg, 2)
        if to_int(end) is None or (to_int(start) is None and not self._is_running()):
            self._missing_argument()

        sp = None
        if start is None:
            sp = peda.getreg("sp")
            start = sp

        dist = end - start
        text = "From %#x%s to %#x: " % (start, " (SP)" if start == sp else "", end)
        text += "%#x bytes, %d qwords%s" % (dist, dist // 8, " (+%d bytes)" % (dist % 8) if (dist % 8 != 0) else "")
        text += ", {:.1f} KB, {:.1f} MB".format(dist / 1024, dist / 1024 / 1024)
        msg(text)

    def session(self, *arg):
        """
        Save/restore a working gdb session to file as a script
        Usage:
            MYNAME save [filename]
            MYNAME restore [filename]
        """
        options = ["save", "restore", "autosave"]
        (option, filename) = normalize_argv(arg, 2)
        if option not in options:
            self._missing_argument()

        if not filename:
            filename = peda.get_config_filename("session")

        if option == "autosave":
            if config.Option.get("autosave") == "on":
                peda.save_session(filename)

        elif option == "restore":
            if peda.restore_session(filename):
                msg("Restored GDB session from file %s" % filename)
            else:
                msg("Failed to restore GDB session")

        elif option == "save":
            if peda.save_session(filename):
                msg("Saved GDB session to file %s" % filename)
            else:
                msg("Failed to save GDB session")

    session.options = ["save", "restore"]

    #################################
    #   Debugging Helper Commands   #
    #################################
    def procinfo(self, *arg):
        """
        Display various info from /proc/pid/
        Usage:
            MYNAME [pid]
        """
        options = ["exe", "fd", "pid", "ppid", "uid", "gid"]

        if peda.getos() != "Linux":
            warning_msg("this command is only available on Linux")

        (pid, ) = normalize_argv(arg, 1)

        if not pid:
            pid = peda.getpid()

        if not pid:
            return

        info = {}
        try:
            info["exe"] = os.path.realpath("/proc/%d/exe" % pid)
        except:
            warning_msg("cannot access /proc/%d/" % pid)
            return

        # fd list
        info["fd"] = {}
        fdlist = os.listdir("/proc/%d/fd" % pid)
        for fd in fdlist:
            rpath = os.readlink("/proc/%d/fd/%s" % (pid, fd))
            sock = re.search("socket:\[(.*)\]", rpath)
            if sock:
                spath = utils.execute_external_command("netstat -aen | grep %s" % sock.group(1))
                if spath:
                    rpath = spath.strip()
            info["fd"][to_int(fd)] = rpath

        # uid/gid, pid, ppid
        info["pid"] = pid
        status = open("/proc/%d/status" % pid).read()
        ppid = re.search("PPid:\s*([^\s]*)", status).group(1)
        info["ppid"] = to_int(ppid) if ppid else -1
        uid = re.search("Uid:\s*([^\n]*)", status).group(1)
        info["uid"] = [to_int(id) for id in uid.split()]
        gid = re.search("Gid:\s*([^\n]*)", status).group(1)
        info["gid"] = [to_int(id) for id in gid.split()]

        for opt in options:
            if opt == "fd":
                for (fd, path) in info[opt].items():
                    msg("fd[%d] -> %s" % (fd, path))
            else:
                msg("%s = %s" % (opt, info[opt]))

    # getfile()
    def getfile(self):
        """
        Get exec filename of current debugged process
        Usage:
            MYNAME
        """
        filename = peda.getfile()
        if filename == None:
            msg("No file specified")
        else:
            msg(filename)

    # getpid()
    def getpid(self):
        """
        Get PID of current debugged process
        Usage:
            MYNAME
        """
        pid = self._is_running()
        msg(pid)

    # disassemble()
    def pdisass(self, *arg):
        """
        Format output of gdb disassemble command with colors
        Usage:
            MYNAME "args for gdb disassemble command"
            MYNAME address /NN: equivalent to "x/NNi address"
        """
        (address, fmt_count) = normalize_argv(arg, 2)
        if isinstance(fmt_count, str) and fmt_count.startswith("/"):
            count = to_int(fmt_count[1:])
            if not count or to_int(address) is None:
                self._missing_argument()
            else:
                code = peda.get_disasm(address, count)
        else:
            code = peda.disassemble(*arg)
        msg(format_disasm_code(code))

    # disassemble_around
    def nearpc(self, *arg):
        """
        Disassemble instructions nearby current PC or given address
        Usage:
            MYNAME [count]
            MYNAME address [count]
                count is maximum 256
        """
        (address, count) = normalize_argv(arg, 2)
        address = to_int(address)
        count = to_int(count)

        if address is not None and address < 0x40000:
            count = address
            address = None

        if address is None:
            address = peda.getreg("pc")

        if count is None:
            code = peda.disassemble_around(address)
        else:
            code = peda.disassemble_around(address, count)

        if code:
            msg(format_disasm_code(code, address))
        else:
            error_msg("invalid $pc address or instruction count")

    def waitfor(self, *arg):
        """
        Try to attach to new forked process; mimic "attach -waitfor"
        Usage:
            MYNAME [cmdname]
            MYNAME [cmdname] -c (auto continue after attached)
        """
        (name, opt) = normalize_argv(arg, 2)
        if name == "-c":
            opt = name
            name = None

        if name is None:
            filename = peda.getfile()
            if filename is None:
                warning_msg("please specify the file to debug or process name to attach")
                return
            else:
                name = os.path.basename(filename)

        msg("Trying to attach to new forked process (%s), Ctrl-C to stop..." % name)
        cmd = "ps axo pid,command | grep %s | grep -v grep" % name
        getpids = []
        out = utils.execute_external_command(cmd)
        for line in out.splitlines():
            getpids += [line.split()[0].strip()]

        while True:
            found = 0
            out = utils.execute_external_command(cmd)
            for line in out.splitlines():
                line = line.split()
                pid = line[0].strip()
                cmdname = line[1].strip()
                if name not in cmdname: continue
                if pid not in getpids:
                    found = 1
                    break

            if found == 1:
                msg("Attching to pid: %s, cmdname: %s" % (pid, cmdname))
                if peda.getpid():
                    peda.execute("detach")
                out = peda.execute("attach %s" % pid, to_string=True)
                msg(out)
                out = peda.execute("file %s" % cmdname, to_string=True)  # reload symbol file
                msg(out)
                if opt == "-c":
                    peda.execute("continue")
                return
            time.sleep(0.5)

    def pltbreak(self, *arg):
        """
        Set breakpoint at PLT functions match name regex
        Usage:
            MYNAME [name]
        """
        (name, ) = normalize_argv(arg, 1)
        if not name:
            name = ""
        headers = peda.elfheader()
        end = headers[".bss"]
        symbols = peda.elfsymbol(name)
        if len(symbols) == 0:
            msg("File not specified or PLT symbols not found")
            return
        else:
            # Traverse symbols in order to have more predictable output
            for symname in sorted(symbols):
                if "plt" not in symname: continue
                if name in symname:  # fixme(longld) bounds checking?
                    line = peda.execute("break %s" % symname, to_string=True)
                    msg("%s (%s)" % (line.strip("\n"), symname))

    def xrefs(self, *arg):
        """
        Search for all call/data access references to a function/variable
        Usage:
            MYNAME pattern
            MYNAME pattern file/mapname
        """
        (search, filename) = normalize_argv(arg, 2)
        if search is None:
            search = ""  # search for all call references
        else:
            search = arg[0]

        if filename is not None:  # get full path to file if mapname is provided
            vmap = peda.get_vmmap(filename)
            if vmap:
                filename = vmap[0][3]

        result = peda.xrefs(search, filename)
        if result:
            if search != "":
                msg("All references to '%s':" % search)
            else:
                msg("All call references")
            for (addr, code) in result:
                msg("%s" % (code))
        else:
            msg("Not found")

    def deactive(self, *arg):
        """
        Bypass a function by ignoring its execution (eg sleep/alarm)
        Usage:
            MYNAME function
            MYNAME function del (re-active)
        """
        (function, action) = normalize_argv(arg, 2)
        if function is None:
            self._missing_argument()

        if to_int(function):
            function = "%#x" % function

        bnum = "$deactive_%s_bnum" % function
        if action and "del" in action:
            peda.execute("delete %s" % bnum)
            peda.execute("set %s = \"void\"" % bnum)
            msg("'%s' re-activated" % function)
            return

        if "void" not in peda.execute("p %s" % bnum, to_string=True):
            out = peda.execute("info breakpoints %s" % bnum, to_string=True)
            if out:
                msg("Already deactivated '%s'" % function)
                msg(out)
                return
            else:
                peda.execute("set %s = \"void\"" % bnum)

        (arch, bits) = peda.getarch()
        if not function.startswith("0x"):  # named function
            symbol = peda.elfsymbol(function)
            if not symbol:
                warning_msg("cannot retrieve info of function '%s'" % function)
                return
            peda.execute("break *%#x" % symbol[function + "@plt"])

        else:  # addressed function
            peda.execute("break *%s" % function)

        peda.execute("set %s = $bpnum" % bnum)
        tmpfd = utils.tmpfile()
        if "i386" in arch:
            tmpfd.write("\n".join(["commands $bpnum", "silent", "set $eax = 0", "return", "continue", "end"]))
        if "64" in arch:
            tmpfd.write("\n".join(["commands $bpnum", "silent", "set $rax = 0", "return", "continue", "end"]))
        tmpfd.flush()
        peda.execute("source %s" % tmpfd.name)
        tmpfd.close()
        out = peda.execute("info breakpoints %s" % bnum, to_string=True)
        if out:
            msg("'%s' deactivated" % function)
            msg(out)

    def unptrace(self, *arg):
        """
        Disable anti-ptrace detection
        Usage:
            MYNAME
            MYNAME del
        """
        (action, ) = normalize_argv(arg, 1)

        self.deactive("ptrace", action)

        if not action and "void" in peda.execute("p $deactive_ptrace_bnum", to_string=True):
            # cannot deactive vi plt entry, try syscall method
            msg("Try to patch 'ptrace' via syscall")
            peda.execute("catch syscall ptrace")
            peda.execute("set $deactive_ptrace_bnum = $bpnum")
            tmpfd = utils.tmpfile()
            (arch, bits) = peda.getarch()
            if "i386" in arch:
                tmpfd.write("\n".join([
                    "commands $bpnum", "silent", "if (*(int*)($esp+4) == 0 || $ebx == 0)", "    set $eax = 0", "end",
                    "continue", "end"
                ]))
            if "64" in arch:
                tmpfd.write("\n".join(
                    ["commands $bpnum", "silent", "if ($rdi == 0)", "    set $rax = 0", "end", "continue", "end"]))
            tmpfd.flush()
            peda.execute("source %s" % tmpfd.name)
            tmpfd.close()
            out = peda.execute("info breakpoints $deactive_ptrace_bnum", to_string=True)
            if out:
                msg("'ptrace' deactivated")
                msg(out)

    # get_function_args()
    def dumpargs(self, *arg):
        """
        Display arguments passed to a function when stopped at a call instruction
        Usage:
            MYNAME [count]
                count: force to display "count args" instead of guessing
        """
        if not self._is_running():
            return

        (count, ) = normalize_argv(arg, 1)

        args = peda.get_function_args(count)
        if args:
            msg("Guessed arguments:")
            for (i, a) in enumerate(args):
                chain = peda.examine_mem_reference(a)
                msg("arg[%d]: %s" % (i, format_reference_chain(chain)))
        else:
            msg("No argument")

    def dumpsyscall_x86(self, *arg):
        """
        Display x86 syacall
        """
        syscall = {}
        syscalltab = {}
        regslist = ["ebx", "ecx", "edx", "esi", "edi", "ebp"]
        with open(os.path.dirname(PEDAFILE) + '/data/x86syscall.csv', "r") as f:
            for row in csv.DictReader(f):
                tmp = {}
                syscall[row['eax']] = row['syscall']
                if len(row['ebx']) > 0:
                    tmp['ebx'] = row['ebx']
                if len(row['ecx']) > 0:
                    tmp['ecx'] = row['ecx']
                if len(row['edx']) > 0:
                    tmp['edx'] = row['edx']
                if len(row['esi']) > 0:
                    tmp['esi'] = row['esi']
                if len(row['edi']) > 0:
                    tmp['edi'] = row['edi']
                if len(row['ebp']) > 0:
                    tmp['ebp'] = row['ebp']
                syscalltab[row['syscall']] = tmp
        f.close()
        nr = peda.getreg("eax")
        try:
            name = syscall[str(nr)]
            arg = syscalltab[name]

            msg(yellow(separator(" System call info "), "light"))
            text = ""
            text2 = ""
            text += name + "("
            for key in regslist:
                if key in arg:
                    value = peda.getreg(key)
                    content = arg[key]
                    text += blue(content, "light") + " = " + hex(value) + ", "
                    chain = peda.examine_mem_reference(value)
                    text2 += "%s : %s\n" % (green(content, "light"), format_reference_chain(chain))
            if text[-1] != '(':
                text = text[:-2] + yellow(")", "light")
                msg(yellow(text, "light"))
                msg(text2.strip())
            else:
                text = text + yellow(")", "light")
                msg(yellow(text, "light"))
            if nr == 0x77:
                msg(yellow(separator(" SROP info "), "light"))
                step = peda.intsize()
                sp = peda.getreg("sp")
                sigcontext_value = []
                sigcontext = [
                    "gs", "fs", "es", "ds", "edi", "esi", "ebp", "esp", "ebx", "edx", "ecx", "eax", "trapno", "err",
                    "eip", "cs", "eflags", "esp_at_signal", "ss", "fpstate", "oldmask", "cr2"
                ]
                for i in range(len(sigcontext)):
                    sigcontext_value.append(peda.examine_mem_value(sp + i * step)[2])
                context = dict(zip(sigcontext, sigcontext_value))
                text = ""
                i = 0
                concern = ["eax", "ebx", "ecx", "edx", "esi", "edi", "eip", "esp", "ebp"]
                for key, value in context.items():
                    if key in concern:
                        text += (yellow(("%14s" % key + ":"), "light") + blue(value))
                    else:
                        text += (green(("%14s" % key + ":"), "light") + blue(value))
                    i += 1
                    if i % 3 == 0:
                        text += "\n"
                msg(text)
        except:
            msg(red("Syscall not fround !!"))

    def dumpsyscall_x64(self, *arg):
        """
        Display x86 syacall
        """
        syscall = {}
        syscalltab = {}
        regslist = ["rdi", "rsi", "rdx", "r10", "r8", "r9"]
        with open(os.path.dirname(PEDAFILE) + '/data/x64syscall.csv', "r") as f:
            for row in csv.DictReader(f):
                tmp = {}
                syscall[row['rax']] = row['syscall']
                if len(row['rdi']) > 0:
                    tmp['rdi'] = row['rdi']
                if len(row['rsi']) > 0:
                    tmp['rsi'] = row['rsi']
                if len(row['rdx']) > 0:
                    tmp['rdx'] = row['rdx']
                if len(row['r10']) > 0:
                    tmp['r10'] = row['r10']
                if len(row['r8']) > 0:
                    tmp['r8'] = row['r8']
                if len(row['r9']) > 0:
                    tmp['r9'] = row['r9']
                syscalltab[row['syscall']] = tmp
        f.close()
        nr = peda.getreg("rax")
        try:
            name = syscall[str(nr)]
            arg = syscalltab[name]

            msg(yellow(separator(" System call info "), "light"))
            text = ""
            text2 = ""
            text += name + "("
            for key in regslist:
                if key in arg:
                    value = peda.getreg(key)
                    content = arg[key]
                    text += blue(content, "light") + " = " + hex(value) + ", "
                    chain = peda.examine_mem_reference(value)
                    text2 += "%s: %s\n" % (green(content, "light"), format_reference_chain(chain))
            if text[-1] != '(':
                text = text[:-2] + yellow(")", "light")
                msg(yellow(text, "light"))
                msg(text2.strip())
            else:
                text = text + yellow(")", "light")
                msg(yellow(text, "light"))

            if nr == 0xf:
                msg(yellow(separator(" SROP info "), "light"))
                step = peda.intsize()
                sp = peda.getreg("sp")
                sigcontext_value = []
                sigcontext = [
                    "uc_flags", "uc_link", "ss_sp", "ss_flags", "ss_size", "r8", "r9", "r10", "r11", "r12", "r13",
                    "r14", "r15", "rdi", "rsi", "rbp", "rbx", "rdx", "rax", "rcx", "rsp", "rip", "eflags", "selector",
                    "err", "trapno", "oldmask", "cr2"
                ]
                for i in range(len(sigcontext)):
                    sigcontext_value.append(peda.examine_mem_value(sp + i * step)[2])
                context = dict(zip(sigcontext, sigcontext_value))
                text = ""
                i = 0
                concern = ["rax", "rbx", "rcx", "rdx", "rsi", "rdi", "rip", "rsp", "rbp"]
                for key, value in context.items():
                    if key in concern:
                        text += (yellow(("%14s" % key + ":"), "light") + blue(value))
                    else:
                        text += (green(("%14s" % key + ":"), "light") + blue(value))
                    i += 1
                    if i % 2 == 0:
                        text += "\n"
                msg(text)
        except:
            msg(red("Syscall not fround !!"))

    def xuntil(self, *arg):
        """
        Continue execution until an address or function
        Usage:
            MYNAME address | function
        """
        (address, ) = normalize_argv(arg, 1)
        if to_int(address) is None:
            peda.execute("tbreak %s" % address)
        else:
            peda.execute("tbreak *%#x" % address)
        pc = peda.getreg("pc")
        if pc is None:
            peda.execute("run")
        else:
            peda.execute("continue")

    def goto(self, *arg):
        """
        Continue execution at an address
        Usage:
            MYNAME address
        """
        (address, ) = normalize_argv(arg, 1)
        if to_int(address) is None:
            self._missing_argument()

        peda.execute("set $pc = %#x" % address)
        peda.execute("stop")

    def skipi(self, *arg):
        """
        Skip execution of next count instructions
        Usage:
            MYNAME [count]
        """
        if not self._is_running():
            return

        (count, ) = normalize_argv(arg, 1)
        if to_int(count) is None:
            count = 1

        next_code = peda.next_inst(peda.getreg("pc"), count)
        if not next_code:
            warning_msg("failed to get next instructions")
            return
        last_addr = next_code[-1][0]
        peda.execute("set $pc = %#x" % last_addr)
        peda.execute("stop")

    def start(self, *arg):
        """
        Start debugged program and stop at most convenient entry
        Usage:
            MYNAME
        """
        # FIXME: both hookpost-run and hookpost-start will be called
        entries = ["main", "__libc_start_main@plt"]

        for e in entries:
            out = peda.execute("tbreak %s" % e, to_string=True)
            if out and "breakpoint" in out:
                peda.execute("run %s" % ' '.join(arg))
                return

        # try ELF entry point or just "run" as the last resort
        is_pie = peda.checksec()['PIE']
        if is_pie:
            peda.save_user_command("hook-stop")  # disable first stop context
            peda.execute("starti %s" % ' '.join(arg))
            peda.restore_user_command("hook-stop")

        elf_entry = peda.elfentry()
        if elf_entry:
            peda.execute("tbreak *%s" % elf_entry)

        if is_pie:
            peda.execute("continue")
        else:
            peda.execute("run %s" % ' '.join(arg))

    # stepuntil()
    def stepuntil(self, *arg):
        """
        Step until a desired instruction in specific memory range
        Usage:
            MYNAME "inst1,inst2" (step to next inst in binary)
            MYNAME "inst1,inst2" mapname1,mapname2
        """
        if not self._is_running():
            return

        (insts, mapname) = normalize_argv(arg, 2)
        if insts is None:
            self._missing_argument()

        peda.save_user_command("hook-stop")  # disable hook-stop to speedup
        msg("Stepping through, Ctrl-C to stop...")
        result = peda.stepuntil(insts, mapname)
        peda.restore_user_command("hook-stop")

        if result:
            peda.execute("stop")

    # wrapper for stepuntil("call")
    def nextcall(self, *arg):
        """
        Step until next 'call' instruction in specific memory range
        Usage:
            MYNAME [keyword] [mapname1,mapname2]
        """
        (keyword, mapname) = normalize_argv(arg, 2)

        if keyword:
            self.stepuntil("call.*%s" % keyword, mapname)
        else:
            self.stepuntil("call", mapname)

    # wrapper for stepuntil("j")
    def nextjmp(self, *arg):
        """
        Step until next 'j*' instruction in specific memory range
        Usage:
            MYNAME [keyword] [mapname1,mapname2]
        """
        (keyword, mapname) = normalize_argv(arg, 2)

        if keyword:
            self.stepuntil("j.*%s" % keyword, mapname)
        else:
            self.stepuntil("j", mapname)

    def profile(self, *arg):
        """
        Simple profiling to count executed instructions in the program
        Usage:
            MYNAME count [keyword]
                default is to count instructions inside the program only
                count = 0: run until end of execution
                keyword: only display stats for instructions matched it
        """
        if not self._is_running():
            return

        (count, keyword) = normalize_argv(arg, 2)
        if count is None:
            self._missing_argument()
        if keyword is None or keyword == "all":
            keyword = ""

        keyword = keyword.replace(" ", "").split(",")

        peda.save_user_command("hook-stop")  # disable hook-stop to speedup
        msg("Stepping %s instructions, Ctrl-C to stop..." % ("%d" % count if count else "all"))

        if count == 0:
            count = -1
        stats = {}
        total = 0
        binmap = peda.get_vmmap("binary")
        try:
            while count != 0:
                pc = peda.getreg("pc")
                if not peda.is_address(pc):
                    break
                code = peda.get_disasm(pc)
                if not code:
                    break
                if peda.is_address(pc, binmap):
                    for k in keyword:
                        if k in code.split(":\t")[-1]:
                            code = code.strip("=>").strip()
                            stats.setdefault(code, 0)
                            stats[code] += 1
                            break
                    peda.execute("stepi", to_string=True)
                else:
                    peda.execute("stepi", to_string=True)
                    peda.execute("finish", to_string=True)
                count -= 1
                total += 1
        except:
            pass

        peda.restore_user_command("hook-stop")
        text = "Executed %d instructions\n" % total
        text += "%s %s\n" % (blue("Run-count", "bold"), blue("Instruction", "bold"))
        for (code, count) in sorted(stats.items(), key=lambda x: x[1], reverse=True):
            text += "%8d: %s\n" % (count, code)
        pager(text)

    @msg.bufferize
    def context_register(self, *arg):
        """
        Display register information of current execution context
        Usage:
            MYNAME
        """
        if not self._is_running():
            return

        pc = peda.getreg("pc")
        # display register info
        msg(yellow(separator(" Registers "), "light"))
        self.xinfo("register")

    @msg.bufferize
    def context_code(self, *arg):
        """
        Display nearby disassembly at $PC of current execution context
        Usage:
            MYNAME [linecount]
        """
        if not self._is_running():
            return

        (count, ) = normalize_argv(arg, 1)
        if count is None:
            count = 8

        msg(yellow(separator(" Code "), "light"))

        pc = peda.getreg("pc")
        self.corrunt_line = pc
        if peda.is_address(pc):
            inst = peda.get_disasm(pc)
        else:  # invalid $PC
            msg("Invalid $PC address: %#x" % pc, "red")
            return

        (arch, bits) = peda.getarch()

        text = ""
        inst = inst.strip().split(":\t")[-1]
        opcode = inst.split()[0]
        m = re.findall(r"\[\S*\]", inst)

        if "aarch64" in arch or "arm" in arch:
            text += peda.disassemble_around(pc, count)

            if opcode == "bl" or opcode == "blx" or opcode == "blr":
                msg(format_disasm_code(text, pc))
                self.dumpargs()
            elif m:
                msg(format_disasm_code(text, pc))
                exp = (m[0][1:-1]).replace(",", "+").replace("#", "")
                if "pc" in exp:
                    exp += "+8"
                val = peda.parse_and_eval(exp)
                if val is not None:
                    chain = peda.examine_mem_reference(val)
                    msg("%s : %s" % (purple(m[0], "light"), format_reference_chain(chain)))
            elif opcode[0] == 'b' or "ret" in opcode or (opcode[0] == 'c' and opcode[-1] == 'z'):
                text = ""
                if "aarch64" in arch:
                    need_jump, jumpto = peda.aarch64_testjump(opcode, inst)
                else:
                    need_jump, jumpto = peda.arm_testjump(opcode, inst)
                if need_jump:  #jump is token
                    code = peda.disassemble_around(pc, count)
                    code = code.splitlines()
                    pc_idx = 999
                    for (idx, line) in enumerate(code):
                        if ("%#x" % pc) in line.split(":")[0]:
                            pc_idx = idx
                        if idx <= pc_idx:
                            text += line + "\n"
                        else:
                            text += " │ %s\n" % line.strip()
                    text = format_disasm_code(text, pc) + "\n"
                    text += " └─>"
                    code = peda.get_disasm(jumpto, count // 2)
                    if not code:
                        code = "   Cannot evaluate jump destination\n"

                    code = code.splitlines()
                    text += red(code[0]) + "\n"
                    for line in code[1:]:
                        text += "       %s\n" % line.strip()
                    if "ret" not in opcode:
                        text += red("JUMP is taken".rjust(79))
                else:
                    text += format_disasm_code(peda.disassemble_around(pc, count), pc)
                    text += "\n" + green("jump is not taken".rjust(79))
                msg(text.rstrip())
            else:
                msg(format_disasm_code(text, pc))
        elif "powerpc" in arch:
            text += peda.disassemble_around(pc, count)
            msg(format_disasm_code(text, pc))
            if "bl" in opcode:
                self.dumpargs()
        else:  # x86
            # remove instruction prefix
            # <memset@plt+4>:	bnd jmp QWORD PTR [rip+0x2f15]
            # TODO: other opcode prefix
            if opcode == 'bnd':
                inst = inst[len(opcode) + 1:]
                opcode = inst.split()[0]
            # stopped at jump
            if opcode[0] == 'j' or opcode == 'ret':
                need_jump, jumpto = peda.testjump(opcode, inst)
                if need_jump:  # JUMP is taken
                    code = peda.disassemble_around(pc, count)
                    code = code.splitlines()
                    pc_idx = 999
                    for (idx, line) in enumerate(code):
                        if ("%#x" % pc) in line.split(":")[0]:
                            pc_idx = idx
                        if idx <= pc_idx:
                            text += line + "\n"
                        else:
                            text += " │ %s\n" % line.strip()
                    text = format_disasm_code(text, pc) + "\n"
                    text += " └─>"
                    code = peda.get_disasm(jumpto, count // 2)
                    if not code:
                        code = "   Cannot evaluate jump destination\n"

                    code = code.splitlines()
                    text += red(code[0]) + "\n"
                    for line in code[1:]:
                        text += "       %s\n" % line.strip()
                    if "ret" not in opcode:
                        text += red("JUMP is taken".rjust(79))
                else:  # JUMP is NOT taken
                    text += format_disasm_code(peda.disassemble_around(pc, count), pc)
                    text += "\n" + green("JUMP is NOT taken".rjust(79))

                msg(text.rstrip())
            # stopped at other instructions
            else:
                text += peda.disassemble_around(pc, count)
                msg(format_disasm_code(text, pc))

                # arch is clear, compare string directly
                if opcode == 'syscall':
                    self.dumpsyscall_x64()
                elif opcode == 'int':
                    self.dumpsyscall_x86()
                elif opcode == 'call':
                    self.dumpargs()

            if m:
                exp = m[0][1:-1]
                if "rip" in exp:
                    ins_size = peda.next_inst(pc)[0][0] - pc
                    exp += "+%d" % ins_size
                val = peda.parse_and_eval(exp)
                if val is not None:
                    chain = peda.examine_mem_reference(val)
                    msg("%s : %s" % (purple(m[0], "light"), format_reference_chain(chain)))

    @msg.bufferize
    def context_stack(self, *arg):
        """
        Display stack of current execution context
        Usage:
            MYNAME [linecount]
        """
        if not self._is_running():
            return

        (count, ) = normalize_argv(arg, 1)

        text = yellow(separator(" Stack "), "light")
        msg(text)
        sp = peda.getreg("sp")
        if peda.is_address(sp):
            self.telescope(sp, count)
        else:
            msg("Invalid $SP address: %#x" % sp, "red")

    @msg.bufferize
    def context_source(self, *arg):
        """
        Display source of current execution context
        """
        (count, ) = normalize_argv(arg, 1)

        sal = gdb.selected_frame().find_sal()
        if sal.symtab is None:
            return

        func_name = gdb.selected_frame().name()
        if func_name is None:  # rare bug
            func_name = ''
        cur_line = sal.line
        filename = sal.symtab.fullname()
        if not os.path.exists(filename):
            return

        if getattr(self, 'filename', '') != filename:
            with open(filename) as f:
                self.source_lines = f.readlines()
            self.filename = filename

        msg(yellow(separator(" Source "), "light"))
        start = max(cur_line - count // 2, 0)
        end = min(cur_line + count // 2, len(self.source_lines))
        if (cur_line - start) < (count // 2):
            end += (count // 2) - cur_line
        if (end - cur_line) < (count // 2):
            start -= count // 2 - (end - cur_line)
            if start < 0:
                start = 0
        for number, line in enumerate(self.source_lines[start:end], start + 1):
            if int(number) == cur_line:
                msg(green("=> {:<4} {}".format(number, line.rstrip("\n")), "light"))
            else:
                msg("   {:<4} {}".format(number, line.rstrip("\n")))

        msg(' {} at {}:{} '.format(yellow(func_name), green(sal.symtab.filename), cur_line).center(utils.get_screen_width() + 20, ' '))  # 20 is hack for color

    def context(self, *arg):
        """
        Display various information of current execution context
        Usage:
            MYNAME [reg,code,stack,all] [code/stack length]
        """
        if not self._is_running():
            return

        (opt, count) = normalize_argv(arg, 2)
        if to_int(count) is None:
            count = config.Option.get("count")
        if opt is None:
            opt = config.Option.get("context")
        if opt == "all":
            opt = "register,code,stack,source"

        context_map = {
            "register": self.context_register,
            "code": self.context_code,
            "stack": self.context_stack,
            "source": self.context_source
        }
        opt = opt.replace(" ", "").split(",")

        if not opt:
            return

        self.clean_screen()

        status = peda.get_status()

        for cont in opt:
            context_map[cont](count)

        if "SIGSEGV" in status:
            if "register" not in opt:
                self.context_register()
            if "stack" not in opt:
                self.context_stack(count)

        msg(separator(), "yellow")
        msg("Legend: %s, %s, %s, %s, value" % (red("code"), blue("data"), green("rodata"), purple("heap")))

        # display stopped reason
        if "SIG" in status:
            msg("Stopped reason: %s" % red(status))

    def clean_screen(self):
        """
        clean screen
        """
        # msg("\x1b[H\x1b[J")
        msg("\x1b[2J\x1b[H")  # origin peda style

    def switch_context(self):
        """
        Switch context layout
        Usage:
            mode 0: register,code,stack
            mode 1: register,source,stack
            mode 2: register,code
        """
        opt = config.Option.get("context")
        if self.mode == 0:
            config.Option.set("context", "register,source,stack")
            self.mode = 1
            self.clean_screen()
            self.context()
        elif self.mode == 1:
            config.Option.set("context", "register,code")
            config.Option.set("count", 16)
            self.mode = 2
            self.clean_screen()
            self.context()
        else:
            config.Option.set("context", "register,code,stack")
            config.Option.set("count", 8)
            self.mode = 0
            self.clean_screen()
            self.context()

    #################################
    #   Memory Operation Commands   #
    #################################
    # get_vmmap()
    def vmmap(self, *arg):
        """
        Get virtual mapping address ranges of section(s) in debugged process
        Usage:
            MYNAME [mapname] (e.g binary, all, libc, stack)
            MYNAME address (find mapname contains this address)
            MYNAME (equiv to cat /proc/pid/maps)
        """
        (mapname, ) = normalize_argv(arg, 1)
        if not self._is_running():
            maps = peda.get_vmmap()
        elif to_int(mapname) is None:
            maps = peda.get_vmmap(mapname)
        else:
            addr = to_int(mapname)
            maps = []
            allmaps = peda.get_vmmap()
            if allmaps is not None:
                for (start, end, perm, name) in allmaps:
                    if addr >= start and addr < end:
                        maps += [(start, end, perm, name)]

        if peda.is_target_remote() and 'ENABLE=' in peda.execute('maintenance packet Qqemu.sstepbits', to_string=True):
            warning_msg('QEMU target detected - vmmap result might not be accurate')
        if maps is not None and len(maps) > 0:
            l = 10 if peda.intsize() == 4 else 18
            msg("%s %s %s\t%s" % ("Start".ljust(l, " "), "End".ljust(l, " "), "Perm", "Name"), "blue", "bold")
            for (start, end, perm, name) in maps:
                color = "red" if "rwx" in perm else None
                msg("%s %s %s\t%s" % (to_address(start).ljust(l, " "), to_address(end).ljust(l, " "), perm, name), color)
        else:
            warning_msg("not found or cannot access procfs")

    # writemem()
    def patch(self, *arg):
        """
        Patch memory start at an address with string/hexstring/int
        Usage:
            MYNAME address (multiple lines input)
            MYNAME address "string"
            MYNAME from_address to_address "string"
            MYNAME (will patch at current $pc)
        """
        (address, data, byte) = normalize_argv(arg, 3)
        address = to_int(address)
        end_address = None
        if address is None:
            address = peda.getreg("pc")

        if byte is not None and to_int(data) is not None:
            end_address, data = to_int(data), byte
            if end_address < address:
                address, end_address = end_address, address

        if data is None:
            data = ""
            while True:
                line = input("patch> ")
                if line.strip() == "": continue
                if line == "end":
                    break
                user_input = line.strip()
                if user_input.startswith("0x"):
                    data += hex2str(user_input)
                else:
                    data += eval("%s" % user_input)

        if to_int(data) is not None:
            data = hex2str(to_int(data), peda.intsize())

        data = utils.to_binary_string(data)
        data = data.replace(b"\\\\", b"\\")
        if end_address:
            data *= (end_address - address + 1) // len(data)
        bytes_ = peda.writemem(address, data)
        if bytes_ >= 0:
            msg("Written %d bytes to %#x" % (bytes_, address))
        else:
            warning_msg("Failed to patch memory, try 'set write on' first for offline patching")

    # dumpmem()
    def dumpmem(self, *arg):
        """
        Dump content of a memory region to raw binary file
        Usage:
            MYNAME file start end
            MYNAME file mapname
        """
        (filename, start, end) = normalize_argv(arg, 3)
        if end is not None and to_int(end):
            if end < start:
                start, end = end, start
            ret = peda.execute("dump memory %s %#x %#x" % (filename, start, end))
            if not ret:
                warning_msg("failed to dump memory")
            else:
                msg("Dumped %d bytes to '%s'" % (end - start, filename))
        elif start is not None:  # dump by mapname
            maps = peda.get_vmmap(start)
            if maps:
                fd = open(filename, "wb")
                count = 0
                for (start, end, _, _) in maps:
                    mem = peda.dumpmem(start, end)
                    if mem is None:  # nullify unreadable memory
                        mem = "\x00" * (end - start)
                    fd.write(mem)
                    count += end - start
                fd.close()
                msg("Dumped %d bytes to '%s'" % (count, filename))
            else:
                warning_msg("invalid mapname")
        else:
            self._missing_argument()

    # loadmem()
    def loadmem(self, *arg):
        """
        Load contents of a raw binary file to memory
        Usage:
            MYNAME file address [size]
        """
        mem = ""
        (filename, address, size) = normalize_argv(arg, 3)
        address = to_int(address)
        size = to_int(size)
        if filename is not None:
            try:
                mem = open(filename, "rb").read()
            except:
                pass
            if mem == "":
                error_msg("cannot read data or filename is empty")
                return
            if size is not None and size < len(mem):
                mem = mem[:size]
            bytes = peda.writemem(address, mem)
            if bytes > 0:
                msg("Written %d bytes to %#x" % (bytes, address))
            else:
                warning_msg("failed to load filename to memory")
        else:
            self._missing_argument()

    # cmpmem()
    def cmpmem(self, *arg):
        """
        Compare content of a memory region with a file
        Usage:
            MYNAME start end file
        """
        (start, end, filename) = normalize_argv(arg, 3)
        if filename is None:
            self._missing_argument()

        try:
            buf = open(filename, "rb").read()
        except:
            error_msg("cannot read data from filename %s" % filename)
            return

        result = peda.cmpmem(start, end, buf)

        if result is None:
            warning_msg("failed to perform comparison")
        elif result == {}:
            msg("mem and filename are identical")
        else:
            msg("--- mem: %s -> %s" % (arg[0], arg[1]), "green", "bold")
            msg("+++ filename: %s" % arg[2], "blue", "bold")
            for (addr, bytes_) in result.items():
                msg("@@ %#x @@" % addr, "red")
                line_1 = "- "
                line_2 = "+ "
                for (mem_val, file_val) in bytes_:
                    m_byte = "%02X " % ord(mem_val)
                    f_byte = "%02X " % ord(file_val)
                    if mem_val == file_val:
                        line_1 += m_byte
                        line_2 += f_byte
                    else:
                        line_1 += green(m_byte)
                        line_2 += blue(f_byte)
                msg(line_1)
                msg(line_2)

    # xormem()
    def xormem(self, *arg):
        """
        XOR a memory region with a key
        Usage:
            MYNAME start end key
        """
        (start, end, key) = normalize_argv(arg, 3)
        if key is None:
            self._missing_argument()

        result = peda.xormem(start, end, key)
        if result is not None:
            msg("XORed data (first 32 bytes):")
            msg('"' + to_hexstr(result[:32]) + '"')

    # searchmem(), searchmem_by_range()
    def searchmem(self, *arg):
        """
        Search for a pattern in memory; support regex search
        Usage:
            MYNAME pattern start end
            MYNAME pattern mapname
        """
        (pattern, start, end) = normalize_argv(arg, 3)
        (pattern, mapname) = normalize_argv(arg, 2)
        if pattern is None:
            self._missing_argument()

        pattern = arg[0]
        result = []
        if end is None and to_int(mapname):
            vmrange = peda.get_vmrange(mapname)
            if vmrange:
                (start, end, _, _) = vmrange

        if end is None:
            msg("Searching for %s in: %s ranges" % (repr(pattern), mapname))
            result = peda.searchmem_by_range(mapname, pattern)
        else:
            msg("Searching for %s in range: %#x - %#x" % (repr(pattern), start, end))
            result = peda.searchmem(start, end, pattern)

        text = peda.format_search_result(result)
        pager(text)

    # search_reference()
    def refsearch(self, *arg):
        """
        Search for all references to a value in memory ranges
        Usage:
            MYNAME value mapname
            MYNAME value (search in all memory ranges)
        """
        (search, mapname) = normalize_argv(arg, 2)
        if search is None:
            self._missing_argument()

        search = arg[0]
        if mapname is None:
            mapname = "all"
        msg("Searching for reference to: %s in: %s ranges" % (repr(search), mapname))
        result = peda.search_reference(search, mapname)

        text = peda.format_search_result(result)
        pager(text)

    # search_address(), search_pointer()
    def lookup(self, *arg):
        """
        Search for all addresses/references to addresses which belong to a memory range
        Usage:
            MYNAME address searchfor belongto
            MYNAME address start end belongto
            MYNAME pointer searchfor belongto
            MYNAME pointer start end belongto
        """
        (option, start, end, belongto) = normalize_argv(arg, 4)
        if option is None:
            self._missing_argument()
        if belongto is None:
            (option, searchfor, belongto) = normalize_argv(arg, 3)
            if belongto is None:
                self._missing_argument()
        else:
            searchfor = (start, end)

        result = []

        if isinstance(searchfor, tuple):
            searchfor_msg = '(%#x, %#x)' % searchfor
        else:
            searchfor_msg = searchfor
        msg("Searching for %ses on: %s pointed to: %s, this may take minutes to complete..." %
            (option, searchfor_msg, belongto))
        if option == "pointer":
            result = peda.search_pointer(searchfor, belongto)
        elif option == "address":
            result = peda.search_address(searchfor, belongto)

        text = peda.format_search_result(result, 0)
        pager(text)

    lookup.options = ["address", "pointer"]

    # examine_mem_reference()
    def telescope(self, *arg):
        """
        Display memory content at an address with smart dereferences
        Usage:
            MYNAME [linecount] (analyze at current $SP)
            MYNAME address [linecount]
        """
        (address, count) = normalize_argv(arg, 2)

        if self._is_running():
            sp = peda.getreg("sp")
        else:
            sp = None

        if count is None:
            count = 8
            if address is None:
                address = sp
            elif address < 0x1000:
                count = address
                address = sp

        if not address:
            return

        step = peda.intsize()
        if not peda.is_address(address):  # cannot determine address
            msg("Invalid $SP address: %#x" % address, "red")
            return

        result = []
        for i in range(count):
            value = address + i * step
            if peda.is_address(value):
                result += [peda.examine_mem_reference(value)]
            else:
                result += [None]
        idx = 0
        text = ""
        for chain in result:
            text += "%04d| " % (idx)
            text += format_reference_chain(chain)
            text += "\n"
            idx += step

        pager(text)

    def eflags(self, *arg):
        """
        Display/set/clear/toggle value of eflags register
        Usage:
            MYNAME
            MYNAME [set|clear|toggle] flagname
        """
        if not self._is_running():
            return

        FLAGS = ["CF", "PF", "AF", "ZF", "SF", "TF", "IF", "DF", "OF"]
        FLAGS_TEXT = ["Carry", "Parity", "Adjust", "Zero", "Sign", "Trap", "Interrupt", "Direction", "Overflow"]

        (option, flagname) = normalize_argv(arg, 2)
        if option and not flagname:
            self._missing_argument()
        elif option is None:  # display eflags
            flags = peda.get_eflags()
            text = ""
            for (i, f) in enumerate(FLAGS):
                if flags[f]:
                    text += "%s " % red(FLAGS_TEXT[i].upper(), "bold")
                else:
                    text += "%s " % green(FLAGS_TEXT[i].lower())
            eflags = peda.getreg("eflags")
            msg("%s: %#x (%s)" % (green("EFLAGS"), eflags, text.strip()))
        elif option == "set":
            peda.set_eflags(flagname, True)
        elif option == "clear":
            peda.set_eflags(flagname, False)
        elif option == "toggle":
            peda.set_eflags(flagname, None)

    eflags.options = ["set", "clear", "toggle"]

    def cpsr(self, *arg):
        """
        Display value of cpsr register
        """
        if not self._is_running():
            return

        FLAGS = ["T", "F", "I", "GE", "V", "C", "Z", "N"]
        FLAGS_TEXT = ["Thumb", "FIQ", "IRQ", "GE", "Overflow", "Carry", "Zero", "Negative"]
        (option, flagname) = normalize_argv(arg, 2)

        if option and not flagname:
            self._missing_argument()

        if option is None:  # display eflags
            flags = peda.get_cpsr()
            text = ""
            for (i, f) in enumerate(FLAGS):
                if flags[f]:
                    text += "%s " % red(FLAGS_TEXT[i].upper(), "bold")
                else:
                    text += "%s " % green(FLAGS_TEXT[i].lower())

            cpsr = peda.getreg("cpsr")
            msg("%s: %#x (%s)" % (green("CPSR"), cpsr, text.strip()))

    cpsr.options = ["set", "clear"]

    def aarch64_cpsr(self, *arg):
        """
        Display value of cpsr register
        """
        if not self._is_running():
            return

        FLAGS = ["F", "I", "A", "D", "V", "C", "Z", "N"]
        FLAGS_TEXT = ["FIQ", "IRQ", "SError", "Debug", "Overflow", "Carry", "Zero", "Negative"]
        (option, flagname) = normalize_argv(arg, 2)

        if option and not flagname:
            self._missing_argument()

        if option is None:  # display eflags
            flags = peda.get_aarch64_cpsr()
            text = ""
            for (i, f) in enumerate(FLAGS):
                if flags[f]:
                    text += "%s " % red(FLAGS_TEXT[i].upper(), "bold")
                else:
                    text += "%s " % green(FLAGS_TEXT[i].lower())

            cpsr = peda.getreg("cpsr")
            msg("%s: %#x (%s)" % (green("CPSR"), cpsr, text.strip()))

    cpsr.options = ["set", "clear"]

    def xinfo(self, *arg):
        """
        Display detail information of address/registers
        Usage:
            MYNAME address
            MYNAME register [reg1 reg2]
        """
        if not self._is_running():
            return

        (address, regname) = normalize_argv(arg, 2)
        if address is None:
            self._missing_argument()

        text = ""

        def get_reg_text(r, v, is_diff=False):
            if is_diff:
                text = red("%s" % r.upper().ljust(3), "light") + ": "
            else:
                text = green("%s" % r.upper().ljust(3)) + ": "
            chain = peda.examine_mem_reference(v)
            text += format_reference_chain(chain)
            text += "\n"
            return text

        (arch, bits) = peda.getarch()
        if str(address).startswith("r"):
            # Register
            regs = peda.getregs(" ".join(arg[1:]))
            if regname is None:
                for r in REGISTERS[arch]:
                    if r in regs:
                        if r in self._diff_regs and self._diff_regs[r] != regs[r]:
                            text += get_reg_text(r, regs[r], is_diff=True)
                        else:
                            text += get_reg_text(r, regs[r])
                        self._diff_regs[r] = regs[r]
            else:
                for (r, v) in sorted(regs.items()):
                    text += get_reg_text(r, v)

            if text:
                msg(text.strip())

            if "x86-64" in arch or "i386" in arch:
                if regname is None or "eflags" in regname:
                    self.eflags()
            else:
                if regname is None or "cpsr" in regname:
                    if "arm" in arch:
                        self.cpsr()
                    elif "aarch64" in arch:
                        self.aarch64_cpsr()
                    else:
                        pass
            return

        elif to_int(address) is None:
            warning_msg("not a register nor an address")
        else:
            # Address
            chain = peda.examine_mem_reference(address, depth=0)
            text += format_reference_chain(chain) + "\n"
            vmrange = peda.get_vmrange(address)
            if vmrange:
                (start, end, perm, name) = vmrange
                text += "Virtual memory mapping:\n"
                text += green("Start : %s\n" % to_address(start))
                text += green("End   : %s\n" % to_address(end))
                binmap = peda.get_vmmap(name) if name != 'mapped' else None
                if binmap:
                    text += yellow("Offset: %#x (%#x in file)\n" % ((address - start), address - binmap[0][0]))
                else:
                    text += yellow("Offset: %#x\n" % (address - start))
                text += red("Perm  : %s\n" % perm)
                text += blue("Name  : %s" % name)
        msg(text)

    xinfo.options = ["register"]

    def strings(self, *arg):
        """
        Display printable strings in memory
        Usage:
            MYNAME start end [minlen]
            MYNAME mapname [minlen]
            MYNAME (display all printable strings in binary - slow)
        """
        (start, end, minlen) = normalize_argv(arg, 3)

        mapname = None
        if start is None:
            mapname = "binary"
        elif to_int(start) is None or (end < start):
            (mapname, minlen) = normalize_argv(arg, 2)

        if minlen is None:
            minlen = 1

        if mapname:
            maps = peda.get_vmmap(mapname)
        else:
            maps = [(start, end, None, None)]

        if not maps:
            warning_msg("failed to get memory map for %s" % mapname)
            return

        text = ""
        regex_pattern = "[%s]{%d,}" % (re.escape(string.printable), minlen)
        for (start, end, _, _) in maps:
            mem = peda.dumpmem(start, end)
            if not mem: continue
            found = re.finditer(regex_pattern.encode('utf-8'), mem)
            if not found: continue

            for m in found:
                text += "%#x: %s\n" % (start + m.start(), utils.string_repr(mem[m.start():m.end()].strip(),
                                                                            show_quotes=False))
        pager(text)

    def sgrep(self, *arg):
        """
        Search for full strings contain the given pattern
        Usage:
            MYNAME pattern start end
            MYNAME pattern mapname
            MYNAME pattern
        """
        (pattern, ) = normalize_argv(arg, 1)

        if pattern is None:
            self._missing_argument()
        arg = list(arg[1:])
        if not arg:
            arg = ["binary"]

        pattern = "[^\x00]*%s[^\x00]*" % pattern
        self.searchmem(pattern, *arg)

    ###############################
    #   Exploit Helper Commands   #
    ###############################
    # elfheader()
    def elfheader(self, *arg):
        """
        Get headers information from debugged ELF file
        Usage:
            MYNAME [header_name]
        """
        (name, ) = normalize_argv(arg, 1)
        if name == 'got':
            name = '.got'
        result = peda.elfheader(name)
        if len(result) == 0:
            warning_msg("%s not found, did you specify the FILE to debug?" % (name if name else "headers"))
        elif len(result) == 1:
            (k, (start, end, type)) = list(result.items())[0]
            msg("%s: %#x - %#x (%s)" % (k, start, end, type))
            if k.startswith(".got"):
                size = peda.intsize()
                self.telescope(start, int((end - start) / size))
        else:
            for (k, (start, end, type)) in sorted(result.items(), key=lambda x: x[1]):
                msg("%s = %#x (%s)" % (k, start, type))

    # readelf_header(), elfheader_solib()
    def readelf(self, *arg):
        """
        Get headers information from an ELF file
        Usage:
            MYNAME mapname [header_name]
            MYNAME filename [header_name]
        """
        (filename, hname) = normalize_argv(arg, 2)
        result = {}
        maps = peda.get_vmmap()
        if filename is None:  # fallback to elfheader()
            result = peda.elfheader()
        else:
            result = peda.elfheader_solib(filename, hname)

        if not result:
            result = peda.readelf_header(filename, hname)
        if len(result) == 0:
            warning_msg("%s or %s not found" % (filename, hname))
        elif len(result) == 1:
            (k, (start, end, type)) = list(result.items())[0]
            msg("%s: %#x - %#x (%s)" % (k, start, end, type))
        else:
            for (k, (start, end, type)) in sorted(result.items(), key=lambda x: x[1]):
                msg("%s = %#x" % (k, start))

    # elfsymbol()
    def elfsymbol(self, *arg):
        """
        Get non-debugging symbol information from an ELF file
        Usage:
            MYNAME symbol_name
        """
        (name, ) = normalize_argv(arg, 1)
        if not peda.getfile():
            warning_msg("please specify a file to debug")
            return

        result = peda.elfsymbol(name)
        if len(result) == 0:
            msg("'%s': no match found" % (name if name else "plt symbols"))
        else:
            if ("%s@got" % name) not in result:
                msg("Found %d symbols" % len(result))
            else:
                msg("Detail symbol info")
            for (k, v) in sorted(result.items(), key=lambda x: x[1]):
                msg("%s = %s" % (k, "%#x" % v if v else repr(v)))

    # checksec()
    def checksec(self, *arg):
        """
        Check for various security options of binary
        For full features, use http://www.trapkit.de/tools/checksec.sh
        Usage:
            MYNAME [file]
        """
        (filename, ) = normalize_argv(arg, 1)
        colorcodes = {
            0: red("disabled"),
            1: green("ENABLED"),
            2: yellow("Partial"),
            3: green("FULL"),
            4: yellow("Dynamic Shared Object"),
        }

        result = peda.checksec(filename)
        if result:
            for (k, v) in sorted(result.items()):
                msg("%s: %s" % (k.ljust(10), colorcodes[v]))

    def nxtest(self, *arg):
        """
        Perform real NX test to see if it is enabled/supported by OS
        Usage:
            MYNAME [address]
        """
        (address, ) = normalize_argv(arg, 1)

        exec_wrapper = peda.execute("show exec-wrapper", to_string=True).split('"')[1]
        if exec_wrapper != "":
            peda.execute("unset exec-wrapper")

        if not peda.getpid():  # start program if not running
            peda.execute("start")

        # set current PC => address, continue
        pc = peda.getreg("pc")
        sp = peda.getreg("sp")
        if not address:
            address = sp
        peda.execute("set $pc = %#x" % address)
        # set value at address => 0xcc
        peda.execute("set *%#x = %#x" % (address, 0xcccccccc))
        peda.execute("set *%#x = %#x" % (address + 4, 0xcccccccc))
        out = peda.execute("continue", to_string=True)
        text = "NX test at %s: " % (to_address(address) if address != sp else "stack")

        if out:
            if "SIGSEGV" in out:
                text += red("Non-Executable")
            elif "SIGTRAP" in out:
                text += green("Executable")
        else:
            text += "Failed to test"

        msg(text)
        # restore exec-wrapper
        if exec_wrapper != "":
            peda.execute("set exec-wrapper %s" % exec_wrapper)

    # search_asm()
    def asmsearch(self, *arg):
        """
        Search for ASM instructions in memory
        Usage:
            MYNAME "asmcode" start end
            MYNAME "asmcode" mapname
        """
        if not self._is_running():
            return

        (asmcode, start, end) = normalize_argv(arg, 3)
        if asmcode is None:
            self._missing_argument()

        asmcode = arg[0]
        result = []
        if end is None:
            mapname = start
            if mapname is None:
                mapname = "binary"
            maps = peda.get_vmmap(mapname)
            msg("Searching for ASM code: %s in: %s ranges" % (repr(asmcode), mapname))
            for (start, end, _, _) in maps:
                if not peda.is_executable(start, maps): continue  # skip non-executable page
                result += peda.search_asm(start, end, asmcode)
        else:
            msg("Searching for ASM code: %s in range: %#x - %#x" % (repr(asmcode), start, end))
            result = peda.search_asm(start, end, asmcode)

        text = "Not found"
        if result:
            text = ""
            for (addr, (byte, code)) in result:
                text += "%s : (%s)\t%s\n" % (to_address(addr), byte.decode('utf-8'), code)
        pager(text)

    # search_jmpcall()
    def jmpcall(self, *arg):
        """
        Search for JMP/CALL instructions in memory
        Usage:
            MYNAME (search all JMP/CALL in current binary)
            MYNAME reg [mapname]
            MYNAME reg start end
        """
        if not self._is_running():
            return

        (reg, start, end) = normalize_argv(arg, 3)
        result = []

        mapname = None
        if start is None:
            mapname = "binary"
        elif end is None:
            mapname = start

        if mapname:
            maps = peda.get_vmmap(mapname)
            for (start, end, _, _) in maps:
                if not peda.is_executable(start, maps): continue
                result += peda.search_jmpcall(start, end, reg)
        else:
            result = peda.search_jmpcall(start, end, reg)

        if not result:
            msg("Not found")
        else:
            text = ""
            for (a, v) in result:
                text += "%#x : %s\n" % (a, v)
            pager(text)

    # cyclic_pattern()
    def pattern_create(self, *arg):
        """
        Generate a cyclic pattern
        Set "pattern" option for basic/extended pattern type
        Usage:
            MYNAME size [file]
        """
        (size, filename) = normalize_argv(arg, 2)
        if size is None:
            self._missing_argument()

        pattern = utils.cyclic_pattern(size)
        if filename is not None:
            open(filename, "wb").write(pattern)
            msg("Writing pattern of %d chars to filename \"%s\"" % (len(pattern), filename))
        else:
            msg(pattern.decode('utf-8'))

    # cyclic_pattern()
    def pattern_offset(self, *arg):
        """
        Search for offset of a value in cyclic pattern
        Set "pattern" option for basic/extended pattern type
        Usage:
            MYNAME value
        """
        (value, ) = normalize_argv(arg, 1)
        if value is None:
            self._missing_argument()

        pos = utils.cyclic_pattern_offset(value)
        if pos is None:
            msg("%s not found in pattern buffer" % value)
        else:
            msg("%s found at offset: %d" % (value, pos))

    # cyclic_pattern(), searchmem_*()
    def pattern_search(self, *arg):
        """
        Search a cyclic pattern in registers and memory
        Set "pattern" option for basic/extended pattern type
        Usage:
            MYNAME
        """

        def nearby_offset(v):
            for offset in range(-128, 128, 4):
                pos = utils.cyclic_pattern_offset(v + offset)
                if pos is not None:
                    return (pos, offset)
            return None

        if not self._is_running():
            return

        reg_result = {}
        regs = peda.getregs()

        # search for registers with value in pattern buffer
        for (r, v) in regs.items():
            if len(to_hex(v)) < 8: continue
            res = nearby_offset(v)
            if res:
                reg_result[r] = res

        if reg_result:
            msg("Registers contain pattern buffer:", "red")
            for (r, (p, o)) in reg_result.items():
                msg("%s+%d found at offset: %d" % (r.upper(), o, p))
        else:
            msg("No register contains pattern buffer")

        # search for registers which point to pattern buffer
        reg_result = {}
        for (r, v) in regs.items():
            if not peda.is_address(v): continue
            chain = peda.examine_mem_reference(v)
            (v, t, vn) = chain[-1]
            if not vn: continue
            o = utils.cyclic_pattern_offset(vn.strip("'").strip('"')[:4])
            if o is not None:
                reg_result[r] = (len(chain), len(vn) - 2, o)

        if reg_result:
            msg("Registers point to pattern buffer:", "yellow")
            for (r, (d, l, o)) in reg_result.items():
                msg("[%s] %s offset %d - size ~%d" % (r.upper(), "-->" * d, o, l))
        else:
            msg("No register points to pattern buffer")

        # search for pattern buffer in memory
        maps = peda.get_vmmap()
        search_result = []
        for (start, end, perm, name) in maps:
            if "w" not in perm: continue  # only search in writable memory
            res = utils.cyclic_pattern_search(peda.dumpmem(start, end))
            for (a, l, o) in res:
                a += start
                search_result += [(a, l, o)]

        sp = peda.getreg("sp")
        if search_result:
            msg("Pattern buffer found at:", "green")
            for (a, l, o) in search_result:
                ranges = peda.get_vmrange(a)
                text = "%s : offset %4d - size %4d" % (to_address(a), o, l)
                if ranges[3] == "[stack]":
                    text += " ($sp + %s [%d dwords])" % (to_hex(a - sp), (a - sp) // 4)
                else:
                    text += " (%s)" % ranges[3]
                msg(text)
        else:
            msg("Pattern buffer not found in memory")

        # search for references to pattern buffer in memory
        ref_result = []
        for (a, l, o) in search_result:
            res = peda.searchmem_by_range("all", "%#x" % a)
            ref_result += [(x[0], a) for x in res]
        if len(ref_result) > 0:
            msg("References to pattern buffer found at:", "blue")
            for (a, v) in ref_result:
                ranges = peda.get_vmrange(a)
                text = "%s : %s" % (to_address(a), to_address(v))
                if ranges[3] == "[stack]":
                    text += " ($sp + %s [%d dwords])" % (to_hex(a - sp), (a - sp) // 4)
                else:
                    text += " (%s)" % ranges[3]
                msg(text)
        else:
            msg("Reference to pattern buffer not found in memory")

    # cyclic_pattern(), writemem()
    def pattern_patch(self, *arg):
        """
        Write a cyclic pattern to memory
        Set "pattern" option for basic/extended pattern type
        Usage:
            MYNAME address size
        """
        (address, size) = normalize_argv(arg, 2)
        if size is None:
            self._missing_argument()

        pattern = utils.cyclic_pattern(size)
        num_bytes_written = peda.writemem(address, pattern)
        if num_bytes_written:
            msg("Written %d chars of cyclic pattern to %#x" % (size, address))
        else:
            msg("Failed to write to memory")

    # cyclic_pattern()
    def pattern_arg(self, *arg):
        """
        Set argument list with cyclic pattern
        Set "pattern" option for basic/extended pattern type
        Usage:
            MYNAME size1 [size2,offset2] ...
        """
        if not arg:
            self._missing_argument()

        arglist = []
        for a in arg:
            (size, offset) = (a + ",").split(",")[:2]
            if offset:
                offset = to_int(offset)
            else:
                offset = 0
            size = to_int(size)
            if size is None or offset is None:
                self._missing_argument()

            # try to generate unique, non-overlapped patterns
            if arglist and offset == 0:
                offset = sum(arglist[-1])
            arglist += [(size, offset)]

        patterns = []
        for (s, o) in arglist:
            patterns += ["\'%s\'" % utils.cyclic_pattern(s, o).decode('utf-8')]
        peda.execute("set arg %s" % " ".join(patterns))
        msg("Set %d arguments to program" % len(patterns))

    # cyclic_pattern()
    def pattern_env(self, *arg):
        """
        Set environment variable with a cyclic pattern
        Set "pattern" option for basic/extended pattern type
        Usage:
            MYNAME ENVNAME size[,offset]
        """
        (env, size) = normalize_argv(arg, 2)
        if size is None:
            self._missing_argument()

        (size, offset) = (arg[1] + ",").split(",")[:2]
        size = to_int(size)
        if offset:
            offset = to_int(offset)
        else:
            offset = 0
        if size is None or offset is None:
            self._missing_argument()

        peda.execute("set env %s %s" % (env, utils.cyclic_pattern(size, offset).decode('utf-8')))
        msg("Set environment %s = cyclic_pattern(%d, %d)" % (env, size, offset))

    def pattern(self, *arg):
        """
        Generate, search, or write a cyclic pattern to memory
        Set "pattern" option for basic/extended pattern type
        Usage:
            MYNAME create size [file]
            MYNAME offset value
            MYNAME search
            MYNAME patch address size
            MYNAME arg size1 [size2,offset2]
            MYNAME env size[,offset]
        """
        options = ["create", "offset", "search", "patch", "arg", "env"]
        (opt, ) = normalize_argv(arg, 1)
        if opt is None or opt not in options:
            self._missing_argument()

        func = getattr(self, "pattern_%s" % opt)
        func(*arg[1:])

    pattern.options = ["create", "offset", "search", "patch", "arg", "env"]

    def substr(self, *arg):
        """
        Search for substrings of a given string/number in memory
        Commonly used for ret2strcpy ROP exploit
        Usage:
            MYNAME "string" start end
            MYNAME "string" [mapname] (default is search in current binary)
        """
        (search, start, end) = normalize_argv(arg, 3)
        if search is None:
            self._missing_argument()

        result = []
        search = arg[0]
        mapname = None
        if start is None:
            mapname = "binary"
        elif end is None:
            mapname = start

        if mapname:
            msg("Searching for sub strings of: %s in: %s ranges" % (repr(search), mapname))
            maps = peda.get_vmmap(mapname)
            for (start, end, perm, _) in maps:
                if perm == "---p":  # skip private range
                    continue
                result = peda.search_substr(start, end, search)
                if result:  # return the first found result
                    break
        else:
            msg("Searching for sub strings of: %s in range: %#x - %#x" % (repr(search), start, end))
            result = peda.search_substr(start, end, search)

        if result:
            msg("# (address, target_offset), # value (address=0xffffffff means not found)")
            offset = 0
            for (k, v) in result:
                msg("(%#x, %d), # %s" % ((0xffffffff if v == -1 else v), offset, utils.string_repr(k)))
                offset += len(k)
        else:
            msg("Not found")

    def assemble(self, *arg):
        """
        On the fly assemble and execute instructions using NASM
        Usage:
            MYNAME [mode] [address]
                mode: -b16 / -b32 / -b64
        """
        (mode, address) = normalize_argv(arg, 2)

        exec_mode = 0
        write_mode = 0
        if to_int(mode) is not None:
            address, mode = mode, None

        (arch, bits) = peda.getarch()
        if mode is None:
            mode = bits
        else:
            mode = to_int(mode[2:])
            if mode not in [16, 32, 64]:
                self._missing_argument()

        if self._is_running() and address == peda.getreg("pc"):
            write_mode = exec_mode = 1

        line = peda.execute("show write", to_string=True)
        if line and "on" in line.split()[-1]:
            write_mode = 1

        if address is None or mode != bits:
            write_mode = exec_mode = 0

        if write_mode:
            msg("Instruction will be written to %#x" % address)
        else:
            msg("Instructions will be written to stdout")

        msg("Type instructions (NASM syntax), one or more per line separated by \";\"")
        msg("End with a line saying just \"end\"")

        if not write_mode:
            address = 0xdeadbeef

        inst_list = []
        inst_code = b""
        # fetch instruction loop
        while True:
            inst = input("iasm|%#x> " % address)
            if inst == "end":
                break
            if inst == "":
                continue
            bincode = peda.assemble(inst, mode)
            size = len(bincode)
            if size == 0:
                continue
            inst_list.append((size, bincode, inst))
            if write_mode:
                peda.writemem(address, bincode)
            # execute assembled code
            if exec_mode:
                peda.execute("stepi %d" % (inst.count(";") + 1))

            address += size
            inst_code += bincode
            msg("hexify: \"%s\"" % to_hexstr(bincode))

        text = Nasm.format_shellcode(b"".join([x[1] for x in inst_list]), mode)
        if text:
            msg("Assembled%s instructions:" % ("/Executed" if exec_mode else ""))
            msg(text)
            msg("hexify: \"%s\"" % to_hexstr(inst_code))

    def shellcode(self, *arg):
        """
        Generate or download common shellcodes.
        Usage:
            MYNAME generate [arch/]platform type [port] [host]
            MYNAME search keyword (use % for any character wildcard)
            MYNAME display shellcodeId (shellcodeId as appears in search results)
            MYNAME zsc [generate customize shellcode]

            For generate option:
                default port for bindport shellcode: 16706 (0x4142)
                default host/port for connect back shellcode: 127.127.127.127/16706
                supported arch: x86
        """

        def list_shellcode():
            """
            List available shellcodes
            """
            text = "Available shellcodes:\n"
            for arch in SHELLCODES:
                for platform in SHELLCODES[arch]:
                    for sctype in SHELLCODES[arch][platform]:
                        text += "    %s/%s %s\n" % (arch, platform, sctype)
            msg(text)

        """ Multiple variable name for different modes """
        (mode, platform, sctype, port, host) = normalize_argv(arg, 5)
        (mode, keyword) = normalize_argv(arg, 2)
        (mode, shellcodeId) = normalize_argv(arg, 2)

        if mode == "generate":
            arch = "x86"
            if platform and "/" in platform:
                (arch, platform) = platform.split("/")

            if platform not in SHELLCODES[arch] or not sctype:
                list_shellcode()
                return
            # utils.dbg_print_vars(arch, platform, sctype, port, host)
            try:
                sc = Shellcode(arch, platform).shellcode(sctype, port, host)
            except Exception as e:
                self._missing_argument()

            if not sc:
                msg("Unknown shellcode")
                return

            hexstr = to_hexstr(sc)
            linelen = 16  # display 16-bytes per line
            i = 0
            text = "# %s/%s/%s: %d bytes\n" % (arch, platform, sctype, len(sc))
            if sctype in ["bindport", "connect"]:
                text += "# port=%s, host=%s\n" % (port if port else '16706', host if host else '127.127.127.127')
            text += "shellcode = (\n"
            while hexstr:
                text += '    "%s"\n' % (hexstr[:linelen * 4])
                hexstr = hexstr[linelen * 4:]
                i += 1
            text += ")"
            msg(text)

        # search shellcodes on shell-storm.org
        elif mode == "search":
            if keyword is None:
                self._missing_argument()

            res_dl = Shellcode().search(keyword)
            if not res_dl:
                msg("Shellcode not found or cannot retrieve the result")
                return

            msg("Found %d shellcodes" % len(res_dl))
            msg("%s\t%s" % (blue("ScId"), blue("Title")))
            text = ""
            for data_d in res_dl:
                text += "[%s]\t%s - %s\n" % (yellow(data_d['ScId']), data_d['ScArch'], data_d['ScTitle'])
            pager(text)

        # download shellcodes from shell-storm.org
        elif mode == "display":
            if to_int(shellcodeId) is None:
                self._missing_argument()

            res = Shellcode().display(shellcodeId)
            if not res:
                msg("Shellcode id not found or cannot retrieve the result")
                return

            msg(res)
        #OWASP ZSC API Z3r0D4y.Com
        elif mode == "zsc":
            'os lists'
            oslist = [
                'linux_x86', 'linux_x64', 'linux_arm', 'linux_mips', 'freebsd_x86', 'freebsd_x64', 'windows_x86',
                'windows_x64', 'osx', 'solaris_x64', 'solaris_x86'
            ]
            'functions'
            joblist = [
                'exec(\'/path/file\')', 'chmod(\'/path/file\',\'permission number\')',
                'write(\'/path/file\',\'text to write\')', 'file_create(\'/path/file\',\'text to write\')',
                'dir_create(\'/path/folder\')', 'download(\'url\',\'filename\')',
                'download_execute(\'url\',\'filename\',\'command to execute\')', 'system(\'command to execute\')'
            ]
            'encode types'
            encodelist = [
                'none', 'xor_random', 'xor_yourvalue', 'add_random', 'add_yourvalue', 'sub_random', 'sub_yourvalue',
                'inc', 'inc_timeyouwant', 'dec', 'dec_timeyouwant', 'mix_all'
            ]
            try:
                while True:
                    for os in oslist:
                        msg('%s %s' % (yellow('[+]'), green(os)))
                    if pyversion == 2:
                        os = input('%s' % blue('os:'))
                    elif pyversion == 3:
                        os = input('%s' % blue('os:'))
                    if os in oslist:  #check if os exist
                        break
                    else:
                        warning_msg("Wrong input! Try Again.")
                while True:
                    for job in joblist:
                        msg('%s %s' % (yellow('[+]'), green(job)))
                    if pyversion == 2:
                        job = raw_input('%s' % blue('job:'))
                    elif pyversion == 3:
                        job = input('%s' % blue('job:'))
                    if job != '':
                        break
                    else:
                        warning_msg("Please enter a function.")
                while True:
                    for encode in encodelist:
                        msg('%s %s' % (yellow('[+]'), green(encode)))
                    if pyversion == 2:
                        encode = raw_input('%s' % blue('encode:'))
                    elif pyversion == 3:
                        encode = input('%s' % blue('encode:'))
                    if encode != '':
                        break
                    else:
                        warning_msg("Please enter a encode type.")
            except (KeyboardInterrupt, SystemExit):
                warning_msg("Aborted by user")
            result = Shellcode().zsc(os, job, encode)
            if result is not None:
                msg(result)
        else:
            self._missing_argument()

    shellcode.options = ["generate", "search", "display", "zsc"]

    def gennop(self, *arg):
        """
        Generate abitrary length NOP sled using given characters
        Usage:
            MYNAME size [chars]
        """
        (size, chars) = normalize_argv(arg, 2)
        if size is None:
            self._missing_argument()

        nops = Shellcode.gennop(size, chars)
        msg(repr(nops))

    def snapshot(self, *arg):
        """
        Save/restore process's snapshot to/from file
        Usage:
            MYNAME save file
            MYNAME restore file
        Warning: this is not thread safe, do not use with multithread program
        """
        options = ["save", "restore"]
        (opt, filename) = normalize_argv(arg, 2)
        if opt not in options:
            self._missing_argument()

        if not filename:
            filename = peda.get_config_filename("snapshot")

        if opt == "save":
            if peda.save_snapshot(filename):
                msg("Saved process's snapshot to filename '%s'" % filename)
            else:
                msg("Failed to save process's snapshot")

        elif opt == "restore":
            if peda.restore_snapshot(filename):
                msg("Restored process's snapshot from filename '%s'" % filename)
                peda.execute("stop")
            else:
                msg("Failed to restore process's snapshot")

    snapshot.options = ["save", "restore"]

    def crashoff(self, *arg):
        """
        Display crash offset when use pattern_create
        Usage:
            MYNAME
        """
        (arch, bits) = peda.getarch()
        pc = peda.getreg("pc")
        if peda.is_address(pc):
            inst = peda.get_disasm(pc)
        else:
            inst = None
        value = peda.getreg("pc")
        if inst:
            if "ret" in inst:
                sp = peda.getreg("sp")
                value = peda.read_int(sp, 4)
        if value is None:
            self._missing_argument()

        pos = utils.cyclic_pattern_offset(value)
        if pos is None:
            msg("%s not found in pattern buffer" % hex(value))
        else:
            msg("%s found at offset: %d" % (hex(value), pos))

    def crashdump(self, *arg):
        """
        Display crashdump info and save to file
        Usage:
            MYNAME [reason_text]
        """
        (reason, ) = normalize_argv(arg, 1)
        if not reason:
            reason = "Interactive dump"

        logname = peda.get_config_filename("crashlog")
        logfd = open(logname, "a")
        config.Option.set("_teefd", logfd)
        msg("[%s]" % "START OF CRASH DUMP".center(78, "-"))
        msg("Timestamp: %s" % time.ctime())
        msg("Reason: %s" % red(reason))

        # exploitability
        pc = peda.getreg("pc")
        if not peda.is_address(pc):
            exp = red("EXPLOITABLE")
        else:
            exp = "Unknown"
        msg("Exploitability: %s" % exp)

        # registers, code, stack
        self.context_register()
        self.context_code(16)
        self.context_stack()

        # backtrace
        msg("[%s]" % "backtrace (innermost 10 frames)".center(78, "-"), "blue")
        msg(peda.execute("backtrace 10", to_string=True))

        msg("[%s]\n" % "END OF CRASH DUMP".center(78, "-"))
        config.Option.set("_teefd", "")
        logfd.close()

    def utils(self, *arg):
        """
        Miscelaneous utilities from utils module
        Usage:
            MYNAME command arg
        """
        (command, carg) = normalize_argv(arg, 2)
        cmds = ["int2hexstr", "list2hexstr", "str2intlist"]
        if not command or command not in cmds or not carg:
            self._missing_argument()

        func = globals()[command]
        if command == "int2hexstr":
            if to_int(carg) is None:
                msg("Not a number")
                return
            result = func(to_int(carg))
            result = to_hexstr(result)

        elif command == "list2hexstr":
            if to_int(carg) is not None:
                msg("Not a list")
                return
            result = func(eval("%s" % carg))
            result = to_hexstr(result)

        elif command == "str2intlist":
            res = func(carg)
            result = "["
            for v in res:
                result += "%s, " % to_hex(v)
            result = result.rstrip(", ") + "]"

        msg(result)

    utils.options = ["int2hexstr", "list2hexstr", "str2intlist"]


###########################################################################
class pedaGDBCommand(gdb.Command):
    """
    Wrapper of gdb.Command for master "peda" command
    """

    def __init__(self, cmdname="peda"):
        self.cmdname = cmdname
        self.__doc__ = pedacmd._get_helptext()
        super(pedaGDBCommand, self).__init__(self.cmdname, gdb.COMMAND_DATA)

    def invoke(self, arg_string, from_tty):
        # do not repeat command
        self.dont_repeat()
        arg = peda.string_to_argv(arg_string)
        if len(arg) < 1:
            pedacmd.help()
        else:
            cmd = arg[0]
            if cmd in pedacmd.commands:
                func = getattr(pedacmd, cmd)
                try:
                    # reset memoized cache
                    utils.reset_cache(sys.modules['__main__'])
                    func(*arg[1:])
                except Exception as e:
                    if config.Option.get("debug") == "on":
                        msg("Exception: %s" % e)
                        traceback.print_exc()
                    peda.restore_user_command("all")
                    pedacmd.help(cmd)
            else:
                msg("Undefined command: %s. Try \"peda help\"" % cmd)

    def complete(self, text, word):
        completion = []
        if text != "":
            cmd = text.split()[0]
            if cmd in pedacmd.commands:
                func = getattr(pedacmd, cmd)
                for opt in func.options:
                    if word in opt:
                        completion += [opt]
            else:
                for cmd in pedacmd.commands:
                    if cmd.startswith(text.strip()):
                        completion += [cmd]
        else:
            for cmd in pedacmd.commands:
                if word in cmd and cmd not in completion:
                    completion += [cmd]
        return completion


###########################################################################
class Alias(gdb.Command):
    """
    Generic alias, create short command names
    This doc should be changed dynamically
    """

    def __init__(self, alias, command, shorttext=1):
        (cmd, opt) = (command + " ").split(" ", 1)
        if cmd == "peda" or cmd == "pead":
            cmd = opt.split(" ")[0]
        if not shorttext:
            self.__doc__ = pedacmd._get_helptext(cmd)
        else:
            self.__doc__ = green("Alias for '%s'" % command)
        self._command = command
        self._alias = alias
        super(Alias, self).__init__(alias, gdb.COMMAND_NONE)

    def invoke(self, args, from_tty):
        self.dont_repeat()
        gdb.execute("%s %s" % (self._command, args))

    def complete(self, text, word):
        completion = []
        cmd = self._command.split("peda ")[1]
        for opt in getattr(pedacmd, cmd).options:  # list of command's options
            if text in opt and opt not in completion:
                completion += [opt]
        if completion != []:
            return completion
        if cmd in ["set", "show"] and text.split()[0] in ["option"]:
            opname = [x for x in config.OPTIONS.keys() if x.startswith(word.strip())]
            if opname != []:
                completion = opname
            else:
                completion = list(config.OPTIONS.keys())
        return completion


###########################################################################
## INITIALIZATION ##
# global instances of PEDA() and PEDACmd()
peda = PEDA()
pedacmd = PEDACmd()
pedacmd.help.__func__.options = pedacmd.commands  # XXX HACK

# register "peda" command in gdb
pedaGDBCommand()
Alias("pead", "peda")  # just for auto correction

# create aliases for subcommands
for cmd in pedacmd.commands:
    func = getattr(pedacmd, cmd)
    func.__func__.__doc__ = func.__doc__.replace("MYNAME", cmd)
    if cmd not in ["help", "show", "set", "enable", "disable"]:
        Alias(cmd, "peda %s" % cmd, 0)

# XXX: is this really needed only for some "useless" commands
# handle SIGINT / Ctrl-C
def sigint_handler(event):
    if peda.enabled:
        peda.restore_user_command("all")

gdb.events.stop.connect(sigint_handler)

# custom hooks
peda.define_user_command("hook-stop", "peda context\n" "session autosave")

# common used shell commands aliases
shellcmds = ["man", "ls", "ps", "grep", "cat", "more", "less", "pkill", "clear", "vi", "nano"]
for cmd in shellcmds:
    Alias(cmd, "shell %s" % cmd)

# custom command aliases, add any alias you want
Alias("phelp", "peda help")
Alias("pset", "peda set")
Alias("pshow", "peda show")
Alias("pbreak", "peda pltbreak")
Alias("pattc", "peda pattern_create")
Alias("patta", "peda pattern_arg")
Alias("patte", "peda pattern_env")
Alias("patts", "peda pattern_search")
# Alias("find", "peda searchmem") # override gdb find command
Alias("stack", "peda telescope $sp")
Alias("viewmem", "peda telescope")
Alias("reg", "peda xinfo register")

# misc gdb settings
peda.execute("set confirm off")
peda.execute("set verbose off")
peda.execute("set output-radix 0x10")
peda.execute("set prompt \001%s\002" % red("\002gdb-peda$ \001"))  # custom prompt
peda.execute("set height 0")  # disable paging
peda.execute("set history expansion on")
peda.execute("set history save on")  # enable history saving
peda.execute("set disassembly-flavor intel")
peda.execute("set backtrace past-main on")
peda.execute("set step-mode on")
peda.execute("set print pretty on")
peda.execute("set pagination off")
peda.execute("handle SIGALRM nostop print nopass")  # ignore SIGALRM
peda.execute("handle SIGSEGV stop   print nopass")  # catch SIGSEGV
