import ctypes
import json
import locale
import multiprocessing
import os
import platform
import textwrap
import sys
from contextlib import redirect_stdout
from datetime import datetime
from io import StringIO
from subprocess import check_output, PIPE, CalledProcessError
import llvmlite.binding as llvmbind
from numba import cuda as cu
from numba import roc
from numba.roc.hlc import hlc, libhlc
from numba.cuda import cudadrv
from numba.cuda.cudadrv.driver import driver as cudriver
from numba.core import config

__ALL__ = ['get_sysinfo', 'display_sysinfo']

# Keys of a `sysinfo` dictionary

# Hardware info
_machine = 'Machine'
_cpu_name, _cpu_count = 'CPU Name', 'CPU Count'
_cpus_allowed, _cpus_list = 'CPUs Allowed', 'List CPUs Allowed'
_cpu_features = 'CPU Features'
_cfs_quota, _cfs_period = 'CFS Quota', 'CFS Period',
_cfs_restrict = 'CFS Restriction'
_mem_total, _mem_available = 'Mem Total', 'Mem Available'
# OS info
_platform_name, _platform_release = 'Platform Name', 'Platform Release'
_os_name, _os_version = 'OS Name', 'OS Version'
_os_spec_version = 'OS Specific Version'
_libc_version = 'Libc Version'
# Python info
_python_comp = 'Python Compiler'
_python_impl = 'Python Implementation'
_python_version = 'Python Version'
_python_locale = 'Python Locale'
# LLVM info
_llvm_version = 'LLVM Version'
# CUDA info
_cu_dev_init = 'CUDA Device Init'
_cu_drv_ver = 'CUDA Driver Version'
_cu_detect_out, _cu_lib_test = 'CUDA Detect Output', 'CUDA Lib Test'
# ROC information
_roc_available, _roc_toolchains = 'ROC Available', 'ROC Toolchain'
_hsa_agents_count, _hsa_agents = 'HSA Agents Count', 'HSA Agents'
_hsa_gpus_count, _hsa_gpus = 'HSA Discrete GPUs Count', 'HSA Discrete GPUs'
# SVML info
_svml_state, _svml_loaded = 'SVML State', 'SVML Lib Loaded'
_llvm_svml_patched = 'LLVM SVML Patched'
_svml_operational = 'SVML Operational'
# Threading layer info
_tbb_thread, _tbb_error = 'TBB Threading', 'TBB Threading Error'
_openmp_thread, _openmp_error = 'OpenMP Threading', 'OpenMP Threading Error'
_openmp_vendor = 'OpenMP vendor'
_wkq_thread, _wkq_error = 'Workqueue Threading', 'Workqueue Threading Error'
# Numba info
_numba_env_vars = 'Numba Env Vars'
# Conda info
_conda_build_ver, _conda_env_ver = 'Conda Build', 'Conda Env'
_conda_platform, _conda_python_ver = 'Conda Platform', 'Conda Python Version'
_conda_root_writable = 'Conda Root Writable'
# Packages info
_inst_pkg = 'Installed Packages'
# Errors and warnings
_errors = 'Errors'
_warnings = 'Warnings'

# Error and warning log
_error_log = []
_warning_log = []


def get_os_spec_info(os_name):
    # Linux man page for `/proc`:
    # http://man7.org/linux/man-pages/man5/proc.5.html

    # Windows documentation for `wmic OS`:
    # https://docs.microsoft.com/en-us/windows/win32/cimwin32prov/cim-operatingsystem

    # MacOS man page for `sysctl`:
    # https://www.unix.com/man-page/osx/3/sysctl/
    # MacOS man page for `vm_stat`:
    # https://www.unix.com/man-page/osx/1/vm_stat/

    class CmdBufferOut(tuple):
        buffer_output_flag = True

    shell_params = {
        'Linux': {
            'cmd': (
                ('cat', '/proc/meminfo'),
                ('cat', '/proc/self/status'),
                CmdBufferOut(('tail', '-vn', '+1',
                              '/sys/fs/cgroup/cpuacct/cpu.cfs_quota_us')),
                CmdBufferOut(('tail', '-vn', '+1',
                              '/sys/fs/cgroup/cpuacct/cpu.cfs_period_us')),
            ),
            'kwds': {
                # output string fragment -> result dict key
                'MemTotal:': _mem_total,
                'MemAvailable:': _mem_available,
                'Cpus_allowed:': _cpus_allowed,
                'Cpus_allowed_list:': _cpus_list,
                '/sys/fs/cgroup/cpuacct/cpu.cfs_quota_us': _cfs_quota,
                '/sys/fs/cgroup/cpuacct/cpu.cfs_period_us': _cfs_period,
            },
        },
        'Windows': {
            'cmd': (
                CmdBufferOut(('wmic', 'OS', 'get', 'TotalVirtualMemorySize')),
                CmdBufferOut(('wmic', 'OS', 'get', 'FreeVirtualMemory')),
            ),
            'kwds': {
                # output string fragment -> result dict key
                'TotalVirtualMemorySize': _mem_total,
                'FreeVirtualMemory': _mem_available,
            },
        },
        'Darwin': {
            'cmd': (
                ('sysctl', 'hw.memsize'),
                ('vm_stat'),
            ),
            'kwds': {
                # output string fragment -> result dict key
                'hw.memsize:': _mem_total,
                'free:': _mem_available,
            },
            'units': {
                _mem_total: 1,  # Size is given in bytes.
                _mem_available: 4096,  # Size is given in 4kB pages.
            },
        },
    }

    # Assuming the shell cmd returns a unique (k, v) pair per line
    # or a unique (k, v) pair spread over several lines:
    # Gather output in a list of strings containing a keyword and some value.
    params = shell_params.get(os_name, {})
    output = []
    for cmd in params.get('cmd', ()):
        try:
            out = check_output(cmd, stderr=PIPE)
        except (OSError, CalledProcessError) as e:
            _error_log.append(f'Error (subprocess): {e}')
            continue
        if hasattr(cmd, 'buffer_output_flag'):
            out = b' '.join(line for line in out.splitlines()) + b'\n'
        output.extend(out.decode().splitlines())

    # Extract (k, output) pairs by searching for keywords in output
    os_spec_info = {}
    kwds = params.get('kwds', {})
    for line in output:
        match = kwds.keys() & line.split()
        if match and len(match) == 1:
            k = kwds[match.pop()]
            os_spec_info[k] = line
        elif len(match) > 1:
            print(f'Ambiguous output: {line}')

    # Try to extract something meaningful from output string
    try:
        # Memory
        units = {_mem_total: 1024, _mem_available: 1024}
        units.update(params.get('units', {}))
        for k in (_mem_total, _mem_available):
            digits = ''.join(d for d in os_spec_info.get(k, '') if d.isdigit())
            os_spec_info[k] = int(digits or 0) * units[k]
        # Accessible CPUs
        split = os_spec_info.get(_cpus_allowed, '').split()
        if split:
            n = split[-1]
            os_spec_info[_cpus_allowed] = str(bin(int(n or 0, 16))).count('1')
        split = os_spec_info.get(_cpus_list, '').split()
        if split:
            os_spec_info[_cpus_list] = split[-1]
        # CFS restrictions
        split = os_spec_info.get(_cfs_quota, '').split()
        if split:
            os_spec_info[_cfs_quota] = float(split[-1])
        split = os_spec_info.get(_cfs_period, '').split()
        if split:
            os_spec_info[_cfs_period] = float(split[-1])
        if os_spec_info.get(_cfs_quota, -1) != -1:
            cfs_quota = os_spec_info.get(_cfs_quota, '')
            cfs_period = os_spec_info.get(_cfs_period, '')
            runtime_amount = cfs_quota / cfs_period
            os_spec_info[_cfs_restrict] = runtime_amount

    except Exception as e:
        _error_log.append(f'Error (format shell output): {e}')

    # Call OS specific functions
    os_specific_funcs = {
        'Linux': {
            _libc_version: lambda: ' '.join(platform.libc_ver())
        },
        'Windows': {
            _os_spec_version: lambda: ' '.join(
                s for s in platform.win32_ver()),
        },
        'Darwin': {
            _os_spec_version: lambda: ''.join(
                i or ' ' for s in tuple(platform.mac_ver()) for i in s),
        },
    }
    key_func = os_specific_funcs.get(os_name, {})
    os_spec_info.update({k: f() for k, f in key_func.items()})
    return os_spec_info


def get_sysinfo():
    os_name = platform.system()

    # Gather the information that shouldn't raise exceptions
    sys_info = {
        _machine: platform.machine(),
        _cpu_name: llvmbind.get_host_cpu_name(),
        _cpu_count: multiprocessing.cpu_count(),
        _platform_name: platform.platform(aliased=True),
        _platform_release: platform.release(),
        _os_name: os_name,
        _os_version: platform.version(),
        _python_comp: platform.python_compiler(),
        _python_impl: platform.python_implementation(),
        _python_version: platform.python_version(),
        _numba_env_vars: {(k, v) for (k, v) in os.environ.items()
                          if k.startswith('NUMBA_')},
        _llvm_version: '.'.join(str(i) for i in llvmbind.llvm_version_info),
        _roc_available: roc.is_available(),
    }

    # CPU features
    try:
        feature_map = llvmbind.get_host_cpu_features()
    except RuntimeError as e:
        _error_log.append(f'Error (CPU features): {e}')
    else:
        features = sorted([key for key, value in feature_map.items() if value])
        sys_info[_cpu_features] = textwrap.fill(' '.join(features), 80)

    # Python locale
    # On MacOSX, getdefaultlocale can raise. Check again if Py > 3.7.5
    try:
        sys_info[_python_locale] = '.'.join(locale.getdefaultlocale())
    except Exception as e:
        _error_log.append(f'Error (locale): {e}')

    # CUDA information
    try:
        cu.list_devices()[0]  # will a device initialise?
    except Exception as e:
        sys_info[_cu_dev_init] = False
        msg_not_found = "CUDA driver library cannot be found"
        msg_disabled_by_user = "CUDA is disabled"
        msg_end = " or no CUDA enabled devices are present."
        msg_generic_problem = "CUDA device intialisation problem."
        msg = getattr(e, 'msg', None)
        if msg is not None:
            if msg_not_found in msg:
                err_msg = msg_not_found + msg_end
            elif msg_disabled_by_user in msg:
                err_msg = msg_disabled_by_user + msg_end
            else:
                err_msg = msg_generic_problem + " Message:" + msg
        else:
            err_msg = msg_generic_problem + " " + str(e)
        # Best effort error report
        _warning_log.append("Warning (cuda): %s\nException class: %s" %
                            (err_msg, str(type(e))))
    else:
        try:
            sys_info[_cu_dev_init] = True

            output = StringIO()
            with redirect_stdout(output):
                cu.detect()
            sys_info[_cu_detect_out] = output.getvalue()
            output.close()

            dv = ctypes.c_int(0)
            cudriver.cuDriverGetVersion(ctypes.byref(dv))
            sys_info[_cu_drv_ver] = dv.value

            output = StringIO()
            with redirect_stdout(output):
                cudadrv.libs.test(sys.platform, print_paths=False)
            sys_info[_cu_lib_test] = output.getvalue()
            output.close()
        except Exception as e:
            _error_log.append(
                "Error (cuda): Probing CUDA failed "
                "(device and driver present, runtime problem?)\n"
                f"(cuda) {type(e)}: {e}")

    # ROC information
    # If no ROC try and report why
    if not sys_info[_roc_available]:
        from numba.roc.hsadrv.driver import hsa
        try:
            hsa.is_available
        except Exception as e:
            msg = str(e)
        else:
            msg = 'No ROC toolchains found.'
        _warning_log.append(f"Warning (roc): Error initialising ROC: {msg}")

    toolchains = []
    try:
        libhlc.HLC()
        toolchains.append('librocmlite library')
    except Exception:
        pass
    try:
        cmd = hlc.CmdLine().check_tooling()
        toolchains.append('ROC command line tools')
    except Exception:
        pass
    sys_info[_roc_toolchains] = toolchains

    try:
        # ROC might not be available due to lack of tool chain, but HSA
        # agents may be listed
        from numba.roc.hsadrv.driver import hsa, dgpu_count

        def decode(x):
            return x.decode('utf-8') if isinstance(x, bytes) else x

        sys_info[_hsa_agents_count] = len(hsa.agents)
        agents = []
        for i, agent in enumerate(hsa.agents):
            agents += {
                'Agent id': i,
                'Vendor': decode(agent.vendor_name),
                'Name': decode(agent.name),
                'Type': agent.device,
            }
        sys_info[_hsa_agents] = agents

        _dgpus = []
        for a in hsa.agents:
            if a.is_component and a.device == 'GPU':
                _dgpus.append(decode(a.name))
        sys_info[_hsa_gpus_count] = dgpu_count()
        sys_info[_hsa_gpus] = ', '.join(_dgpus)
    except Exception as e:
        _warning_log.append(
            "Warning (roc): No HSA Agents found, "
            f"encountered exception when searching: {e}")

    # SVML information
    # Replicate some SVML detection logic from numba.__init__ here.
    # If SVML load fails in numba.__init__ the splitting of the logic
    # here will help diagnosing the underlying issue.
    svml_lib_loaded = True
    try:
        if sys.platform.startswith('linux'):
            llvmbind.load_library_permanently("libsvml.so")
        elif sys.platform.startswith('darwin'):
            llvmbind.load_library_permanently("libsvml.dylib")
        elif sys.platform.startswith('win'):
            llvmbind.load_library_permanently("svml_dispmd")
        else:
            svml_lib_loaded = False
    except Exception:
        svml_lib_loaded = False
    func = getattr(llvmbind.targets, "has_svml", None)
    sys_info[_llvm_svml_patched] = func() if func else False
    sys_info[_svml_state] = config.USING_SVML
    sys_info[_svml_loaded] = svml_lib_loaded
    sys_info[_svml_operational] = all((
        sys_info[_svml_state],
        sys_info[_svml_loaded],
        sys_info[_llvm_svml_patched],
    ))

    # Check which threading backends are available.
    def parse_error(e, backend):
        # parses a linux based error message, this is to provide feedback
        # and hide user paths etc
        try:
            path, problem, symbol = [x.strip() for x in e.msg.split(':')]
            extn_dso = os.path.split(path)[1]
            if backend in extn_dso:
                return "%s: %s" % (problem, symbol)
        except Exception:
            pass
        return "Unknown import problem."

    try:
        from numba.np.ufunc import tbbpool
        sys_info[_tbb_thread] = True
    except ImportError as e:
        # might be a missing symbol due to e.g. tbb libraries missing
        sys_info[_tbb_thread] = False
        sys_info[_tbb_error] = parse_error(e, 'tbbpool')

    try:
        from numba.np.ufunc import omppool
        sys_info[_openmp_thread] = True
        sys_info[_openmp_vendor] = omppool.openmp_vendor
    except ImportError as e:
        sys_info[_openmp_thread] = False
        sys_info[_openmp_error] = parse_error(e, 'omppool')

    try:
        from numba.np.ufunc import workqueue
        sys_info[_wkq_thread] = True
    except ImportError as e:
        sys_info[_wkq_thread] = True
        sys_info[_wkq_error] = parse_error(e, 'workqueue')

    # Look for conda and installed packages information
    cmd = ('conda', 'info', '--json')
    try:
        conda_out = check_output(cmd)
    except Exception as e:
        _warning_log.append(f'Warning: Conda not available.\n Error was {e}\n')
        # Conda is not available, try pip list to list installed packages
        cmd = (sys.executable, '-m', 'pip', 'list')
        try:
            reqs = check_output(cmd)
        except Exception as e:
            _error_log.append(f'Error (pip): {e}')
        else:
            sys_info[_inst_pkg] = reqs.decode().splitlines()

    else:
        jsond = json.loads(conda_out.decode())
        keys = {
            'conda_build_version': _conda_build_ver,
            'conda_env_version': _conda_env_ver,
            'platform': _conda_platform,
            'python_version': _conda_python_ver,
            'root_writable': _conda_root_writable,
        }
        for conda_k, sysinfo_k in keys.items():
            sys_info[sysinfo_k] = jsond.get(conda_k, 'N/A')

        # Get info about packages in current environment
        cmd = ('conda', 'list')
        try:
            conda_out = check_output(cmd)
        except CalledProcessError as e:
            _error_log.append(f'Error (conda): {e}')
        else:
            data = conda_out.decode().splitlines()
            sys_info[_inst_pkg] = [l for l in data if not l.startswith('#')]

    sys_info.update(get_os_spec_info(os_name))
    sys_info[_errors] = _error_log
    sys_info[_warnings] = _warning_log
    return sys_info


def display_sysinfo():
    class DisplayMap(dict):
        display_map_flag = True

    class DisplaySeq(tuple):
        display_seq_flag = True

    class DisplaySeqMaps(tuple):
        display_seqmaps_flag = True

    start = datetime.utcnow()
    info = get_sysinfo()
    delta = datetime.utcnow() - start

    fmt = "%-45s : %-s"
    MB = 1024**2
    template = (
        ("-" * 80,),
        ("__Time Stamp__",),
        ("Report started", start),
        ("Running time (s)", delta.total_seconds()),
        ("",),
        ("__Hardware Information__",),
        ("Machine", info[_machine]),
        ("CPU Name", info[_cpu_name]),
        ("CPU Count", info[_cpu_count]),
        ("Number of accessible CPUs", info.get(_cpus_allowed, 'N/A')),
        ("List of accessible CPUs cores", info.get(_cpus_list, 'N/A')),
        ("CFS Restrictions (CPUs worth of runtime)",
            info.get(_cfs_restrict, 'None')),
        ("",),
        ("CPU Features:",),
        (info.get(_cpu_features, 'N/A'),),
        ("",),
        ("Memory Total (MB)", info.get(_mem_total, 0) // MB or '?'),
        ("Memory Available (MB)", info.get(_mem_available, 0) // MB or '?'),
        ("",),
        ("__OS Information__",),
        ("Platform Name", info[_platform_name]),
        ("Platform Release", info[_platform_release]),
        ("OS Name", info[_os_name]),
        ("OS Version", info[_os_version]),
        ("OS Specific Version", info.get(_os_spec_version, 'N/A')),
        ("Libc Version", info.get(_libc_version, 'N/A')),
        ("",),
        ("__Python Information__",),
        DisplayMap({k: v for k, v in info.items() if k.startswith('Python')}),
        ("",),
        ("__LLVM Information__",),
        ("LLVM Version", info[_llvm_version]),
        ("",),
        ("__CUDA Information__",),
        ("CUDA Device Initialized", info[_cu_dev_init]),
        ("CUDA Driver Version", info.get(_cu_drv_ver, 'N/A')),
        ("CUDA Detect Output:",),
        (info.get(_cu_detect_out, "None"),),
        ("CUDA Librairies Test Output:",),
        (info.get(_cu_lib_test, "None"),),
        ("",),
        ("__ROC information__",),
        ("ROC Available", info[_roc_available]),
        ("ROC Toolchains", info[_roc_toolchains] or 'None'),
        ("HSA Agents Count", info.get(_hsa_agents_count, 0)),
        ("HSA Agents:",),
        (DisplaySeqMaps(info.get(_hsa_agents, {})) or ('None',)),
        ('HSA Discrete GPUs Count', info.get(_hsa_gpus_count, 0)),
        ('HSA Discrete GPUs', info.get(_hsa_gpus, 'None')),
        ("",),
        ("__SVML Information__",),
        ("SVML State, config.USING_SVML", info[_svml_state]),
        ("SVML Library Loaded", info[_svml_loaded]),
        ("llvmlite Using SVML Patched LLVM", info[_llvm_svml_patched]),
        ("SVML Operational", info[_svml_operational]),
        ("",),
        ("__Threading Layer Information__",),
        ("TBB Threading Layer Available", info[_tbb_thread]),
        ("+-->TBB imported successfully." if info[_tbb_thread]
            else f"+--> Disabled due to {info.get(_tbb_error, '?')}",),
        ("OpenMP Threading Layer Available", info[_openmp_thread]),
        (f"+-->Vendor: {info[_openmp_vendor]}" if info[_openmp_thread]
            else f"+--> Disabled due to {info.get(_openmp_error, '?')}",),
        ("Workqueue Threading Layer Available", info[_wkq_thread]),
        ("+-->Workqueue imported successfully." if info[_wkq_thread]
            else f"+--> Disabled due to {info.get(_wkq_error, '?')}",),
        ("",),
        ("__Numba Environment Variable Information__",),
        (DisplayMap(info[_numba_env_vars]) or ('None found.',)),
        ("",),
        ("__Conda Information__",),
        (DisplayMap({k: v for k, v in info.items()
                     if k.startswith('Conda')}) or ("Conda not available.",)),
        ("",),
        ("__Installed Packages__",),
        DisplaySeq(info.get(_inst_pkg, ("Couldn t retrieve packages info.",))),
        ("",),
        ("__Error log__" if _error_log else "No errors reported.",),
        DisplaySeq(_error_log),
        ("",),
        ("__Warning log__" if _warning_log else "No warnings reported.",),
        DisplaySeq(_warning_log),
        ("-" * 80,),
        ("If requested, please copy and paste the information between\n"
         "the dashed (----) lines, or from a given specific section as\n"
         "appropriate.\n\n"
         "=============================================================\n"
         "IMPORTANT: Please ensure that you are happy with sharing the\n"
         "contents of the information present, any information that you\n"
         "wish to keep private you should remove before sharing.\n"
         "=============================================================\n",),
    )
    for t in template:
        if hasattr(t, 'display_seq_flag'):
            print(*t, sep='\n')
        elif hasattr(t, 'display_map_flag'):
            print(*tuple(fmt % (k, v) for (k, v) in t.items()), sep='\n')
        elif hasattr(t, 'display_seqmaps_flag'):
            for d in t:
                print(*tuple(fmt % ('\t' + k, v) for (k, v) in d.items()),
                      sep='\n', end='\n')
        elif len(t) == 2:
            print(fmt % t)
        else:
            print(*t)


if __name__ == '__main__':
    display_sysinfo()
