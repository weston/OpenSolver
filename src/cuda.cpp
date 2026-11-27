// The CUDA backend of the discrete-GPU solve (gpu_backend.hpp), for NVIDIA GPUs. Both the driver API
// (nvcuda.dll, libcuda.so.1, part of the NVIDIA driver) and NVRTC (nvrtc64_*.dll, libnvrtc.so.*, the
// CUDA runtime compiler: from the CUDA toolkit, or shipped next to the solver) are loaded at run time,
// so the solver builds without the CUDA toolkit and starts on a machine without either. The kernels
// are the OpenCL ones (postflop.cl, flop_table.cl) compiled as CUDA C++ through gpu_cuda.h, for the
// device's own architecture, on first use.
#include "gpu.hpp"

#if !(defined(__APPLE__) && defined(__aarch64__))

#if defined(_WIN32)
#ifndef NOMINMAX
#define NOMINMAX
#endif
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#define WSOLVER_CU_API __stdcall
#else
#include <dlfcn.h>
#define WSOLVER_CU_API
#endif

#include <algorithm>
#include <cstring>
#include <new>
#include <stdexcept>

#include "gpu_backend.hpp"

namespace pio {
namespace gpu {
namespace {

// ----- the CUDA driver API and NVRTC, as much of them as is used ---------------------------------
typedef int CUresult;
typedef int CUdevice;
typedef struct CUctx_st* CUcontext;
typedef struct CUmod_st* CUmodule;
typedef struct CUfunc_st* CUfunction;
typedef struct CUstream_st* CUstream;
typedef struct CUevent_st* CUevent;
typedef unsigned long long CUdeviceptr;
typedef struct _nvrtcProgram* nvrtcProgram;
typedef int nvrtcResult;

constexpr CUresult CUDA_SUCCESS = 0, CUDA_ERROR_OUT_OF_MEMORY = 2;
constexpr int CU_DEVICE_ATTRIBUTE_MULTIPROCESSOR_COUNT = 16, CU_DEVICE_ATTRIBUTE_CLOCK_RATE = 13,
              CU_DEVICE_ATTRIBUTE_COMPUTE_CAPABILITY_MAJOR = 75, CU_DEVICE_ATTRIBUTE_COMPUTE_CAPABILITY_MINOR = 76,
              CU_DEVICE_ATTRIBUTE_MAX_SHARED_MEMORY_PER_BLOCK = 8,
              CU_DEVICE_ATTRIBUTE_MAX_SHARED_MEMORY_PER_BLOCK_OPTIN = 97;
constexpr int CU_FUNC_ATTRIBUTE_MAX_THREADS_PER_BLOCK = 0, CU_FUNC_ATTRIBUTE_SHARED_SIZE_BYTES = 1,
              CU_FUNC_ATTRIBUTE_MAX_DYNAMIC_SHARED_SIZE_BYTES = 8;

#if defined(_WIN32)
using Library = HMODULE;
Library open_library(const char* name) {
  return LoadLibraryExA(name, nullptr, std::strchr(name, '\\') ? LOAD_WITH_ALTERED_SEARCH_PATH : 0);
}
void* symbol(Library lib, const char* name) { return reinterpret_cast<void*>(GetProcAddress(lib, name)); }
#else
using Library = void*;
Library open_library(const char* name) { return dlopen(name, RTLD_NOW | RTLD_LOCAL); }
void* symbol(Library lib, const char* name) { return dlsym(lib, name); }
#endif

struct Driver {
  CUresult(WSOLVER_CU_API* Init)(unsigned);
  CUresult(WSOLVER_CU_API* DeviceGetCount)(int*);
  CUresult(WSOLVER_CU_API* DeviceGet)(CUdevice*, int);
  CUresult(WSOLVER_CU_API* DeviceGetName)(char*, int, CUdevice);
  CUresult(WSOLVER_CU_API* DeviceTotalMem)(size_t*, CUdevice);
  CUresult(WSOLVER_CU_API* DeviceGetAttribute)(int*, int, CUdevice);
  CUresult(WSOLVER_CU_API* CtxCreate)(CUcontext*, unsigned, CUdevice);
  CUresult(WSOLVER_CU_API* CtxSetCurrent)(CUcontext);
  CUresult(WSOLVER_CU_API* ModuleLoadData)(CUmodule*, const void*);
  CUresult(WSOLVER_CU_API* ModuleGetFunction)(CUfunction*, CUmodule, const char*);
  CUresult(WSOLVER_CU_API* FuncGetAttribute)(int*, int, CUfunction);
  CUresult(WSOLVER_CU_API* FuncSetAttribute)(CUfunction, int, int);
  CUresult(WSOLVER_CU_API* MemAlloc)(CUdeviceptr*, size_t);
  CUresult(WSOLVER_CU_API* MemFree)(CUdeviceptr);
  CUresult(WSOLVER_CU_API* MemcpyHtoDAsync)(CUdeviceptr, const void*, size_t, CUstream);
  CUresult(WSOLVER_CU_API* MemcpyDtoHAsync)(void*, CUdeviceptr, size_t, CUstream);
  CUresult(WSOLVER_CU_API* MemsetD32Async)(CUdeviceptr, unsigned, size_t, CUstream);
  CUresult(WSOLVER_CU_API* LaunchKernel)(CUfunction, unsigned, unsigned, unsigned, unsigned, unsigned, unsigned,
                                         unsigned, CUstream, void**, void**);
  CUresult(WSOLVER_CU_API* StreamCreate)(CUstream*, unsigned);
  CUresult(WSOLVER_CU_API* StreamSynchronize)(CUstream);
  CUresult(WSOLVER_CU_API* EventCreate)(CUevent*, unsigned);
  CUresult(WSOLVER_CU_API* EventRecord)(CUevent, CUstream);
  CUresult(WSOLVER_CU_API* EventSynchronize)(CUevent);
  CUresult(WSOLVER_CU_API* EventElapsedTime)(float*, CUevent, CUevent);
  CUresult(WSOLVER_CU_API* EventDestroy)(CUevent);
  CUresult(WSOLVER_CU_API* GetErrorString)(CUresult, const char**);

  bool load() {
#if defined(_WIN32)
    Library lib = open_library("nvcuda.dll");
#else
    Library lib = open_library("libcuda.so.1");
    if (!lib) lib = open_library("libcuda.so");
#endif
    if (!lib) return false;
    bool ok = true;
    auto get = [&](auto& fn, const char* name) {
      void* p = symbol(lib, name);
      std::memcpy(&fn, &p, sizeof p);
      ok = ok && p;
    };
    get(Init, "cuInit");
    get(DeviceGetCount, "cuDeviceGetCount");
    get(DeviceGet, "cuDeviceGet");
    get(DeviceGetName, "cuDeviceGetName");
    get(DeviceTotalMem, "cuDeviceTotalMem_v2");
    get(DeviceGetAttribute, "cuDeviceGetAttribute");
    get(CtxCreate, "cuCtxCreate_v2");
    get(CtxSetCurrent, "cuCtxSetCurrent");
    get(ModuleLoadData, "cuModuleLoadData");
    get(ModuleGetFunction, "cuModuleGetFunction");
    get(FuncGetAttribute, "cuFuncGetAttribute");
    get(FuncSetAttribute, "cuFuncSetAttribute");
    get(MemAlloc, "cuMemAlloc_v2");
    get(MemFree, "cuMemFree_v2");
    get(MemcpyHtoDAsync, "cuMemcpyHtoDAsync_v2");
    get(MemcpyDtoHAsync, "cuMemcpyDtoHAsync_v2");
    get(MemsetD32Async, "cuMemsetD32Async");
    get(LaunchKernel, "cuLaunchKernel");
    get(StreamCreate, "cuStreamCreate");
    get(StreamSynchronize, "cuStreamSynchronize");
    get(EventCreate, "cuEventCreate");
    get(EventRecord, "cuEventRecord");
    get(EventSynchronize, "cuEventSynchronize");
    get(EventElapsedTime, "cuEventElapsedTime");
    get(EventDestroy, "cuEventDestroy_v2");
    get(GetErrorString, "cuGetErrorString");
    return ok;
  }
};

struct Nvrtc {
  nvrtcResult (*CreateProgram)(nvrtcProgram*, const char*, const char*, int, const char* const*, const char* const*);
  nvrtcResult (*CompileProgram)(nvrtcProgram, int, const char* const*);
  nvrtcResult (*GetProgramLogSize)(nvrtcProgram, size_t*);
  nvrtcResult (*GetProgramLog)(nvrtcProgram, char*);
  nvrtcResult (*GetPTXSize)(nvrtcProgram, size_t*);
  nvrtcResult (*GetPTX)(nvrtcProgram, char*);
  nvrtcResult (*DestroyProgram)(nvrtcProgram*);
  nvrtcResult (*Version)(int*, int*);

  // NVRTC next to the solver first (an installer may ship it), then the CUDA toolkit's.
  bool load() {
    std::vector<std::string> names;
#if defined(_WIN32)
    const char* versions[] = {"130_0", "120_0", "112_0", "111_0", "110_0"};
    for (const char* v : versions) names.push_back(std::string("nvrtc64_") + v + ".dll");
    if (const char* home = std::getenv("CUDA_PATH")) {
      const size_t n = names.size();
      for (size_t i = 0; i < n; ++i) {
        names.push_back(std::string(home) + "\\bin\\x64\\" + names[i]);
        names.push_back(std::string(home) + "\\bin\\" + names[i]);
      }
    }
#else
    for (const char* v : {"libnvrtc.so.13", "libnvrtc.so.12", "libnvrtc.so.11.2", "libnvrtc.so"}) names.push_back(v);
    for (const char* v : {"/usr/local/cuda/lib64/libnvrtc.so"}) names.push_back(v);
#endif
    Library lib = nullptr;
    for (const auto& name : names) {
#if defined(_WIN32)
      // NVRTC loads its builtins (nvrtc-builtins64_<version>.dll) by name when it compiles: from a
      // toolkit folder that is not on the search path, load them first, from beside it.
      const size_t slash = name.rfind('\\');
      if (slash != std::string::npos) {
        WIN32_FIND_DATAA found;
        HANDLE h = FindFirstFileA((name.substr(0, slash + 1) + "nvrtc-builtins64_*.dll").c_str(), &found);
        if (h != INVALID_HANDLE_VALUE) {
          do open_library((name.substr(0, slash + 1) + found.cFileName).c_str());
          while (FindNextFileA(h, &found));
          FindClose(h);
        }
      }
#endif
      if ((lib = open_library(name.c_str()))) break;
    }
    if (!lib) return false;
    bool ok = true;
    auto get = [&](auto& fn, const char* name) {
      void* p = symbol(lib, name);
      std::memcpy(&fn, &p, sizeof p);
      ok = ok && p;
    };
    get(CreateProgram, "nvrtcCreateProgram");
    get(CompileProgram, "nvrtcCompileProgram");
    get(GetProgramLogSize, "nvrtcGetProgramLogSize");
    get(GetProgramLog, "nvrtcGetProgramLog");
    get(GetPTXSize, "nvrtcGetPTXSize");
    get(GetPTX, "nvrtcGetPTX");
    get(DestroyProgram, "nvrtcDestroyProgram");
    get(Version, "nvrtcVersion");
    return ok;
  }
};

class Cuda final : public Backend {
 public:
  Driver cu{};
  Nvrtc rtc{};
  CUdevice dev = 0;
  CUcontext ctx = nullptr;
  CUstream stream = nullptr;
  CUmodule modules[kVariants] = {};
  int arch = 0;  // compute capability, 10 * major + minor
  size_t shared_limit = 48 * 1024;  // dynamic + static shared memory a block can have (opted in)
  std::map<std::string, CUfunction> functions[kVariants];
  struct Info {
    size_t width, local;
  };
  std::map<CUfunction, Info> info_by_function;

  const char* api() const override { return "CUDA"; }

  void check(CUresult err, const char* what) const {
    if (err == CUDA_SUCCESS) return;
    if (err == CUDA_ERROR_OUT_OF_MEMORY) throw std::bad_alloc();
    const char* text = nullptr;
    if (cu.GetErrorString) cu.GetErrorString(err, &text);
    throw std::runtime_error(std::string("CUDA ") + what + " failed (" + std::to_string(err) + (text ? std::string(": ") + text : "") + ")");
  }
  void current() const { check(cu.CtxSetCurrent(ctx), "context"); }

  void init() {
    if (!cu.load()) throw std::runtime_error("no NVIDIA driver");
    check(cu.Init(0), "init");
    int count = 0;
    check(cu.DeviceGetCount(&count), "device count");
    if (!count) throw std::runtime_error("no CUDA device");
    // the most compute: multiprocessors times clock
    double best = -1;
    for (int i = 0; i < count; ++i) {
      CUdevice d = 0;
      if (cu.DeviceGet(&d, i) != CUDA_SUCCESS) continue;
      int sms = 0, clock = 0, major = 0;
      cu.DeviceGetAttribute(&sms, CU_DEVICE_ATTRIBUTE_MULTIPROCESSOR_COUNT, d);
      cu.DeviceGetAttribute(&clock, CU_DEVICE_ATTRIBUTE_CLOCK_RATE, d);
      cu.DeviceGetAttribute(&major, CU_DEVICE_ATTRIBUTE_COMPUTE_CAPABILITY_MAJOR, d);
      if (major < 5) continue;
      const double score = double(sms) * std::max(clock, 1);
      if (score > best) {
        best = score;
        dev = d;
      }
    }
    if (best < 0) throw std::runtime_error("no CUDA device of compute capability 5.0 or later");
    char name[256] = {};
    cu.DeviceGetName(name, sizeof name - 1, dev);
    device_name = name;
    size_t total = 0;
    check(cu.DeviceTotalMem(&total, dev), "memory size");
    memory = max_alloc = total;
    int major = 0, minor = 0, optin = 0, shared = 0;
    cu.DeviceGetAttribute(&major, CU_DEVICE_ATTRIBUTE_COMPUTE_CAPABILITY_MAJOR, dev);
    cu.DeviceGetAttribute(&minor, CU_DEVICE_ATTRIBUTE_COMPUTE_CAPABILITY_MINOR, dev);
    cu.DeviceGetAttribute(&shared, CU_DEVICE_ATTRIBUTE_MAX_SHARED_MEMORY_PER_BLOCK, dev);
    if (cu.DeviceGetAttribute(&optin, CU_DEVICE_ATTRIBUTE_MAX_SHARED_MEMORY_PER_BLOCK_OPTIN, dev) != CUDA_SUCCESS) optin = 0;
    arch = 10 * major + minor;
    shared_limit = size_t(std::max(shared, optin));
    if (!rtc.load()) throw std::runtime_error("no NVRTC (the CUDA runtime compiler, nvrtc64_*.dll / libnvrtc.so)");
    check(cu.CtxCreate(&ctx, 0, dev), "context");
    check(cu.StreamCreate(&stream, 0), "stream");
    module(kFloatState);  // a toolkit that cannot build the kernels offers no GPU
  }

  // PTX for the device's architecture; the driver finishes it. An NVRTC older than the device does not
  // know its architecture: then the newest it does, which the driver still runs.
  CUmodule module(Variant variant) {
    if (modules[variant]) return modules[variant];
    ProfileScope ps(*this, "setup: compile kernels");
    std::string log, ptx;
    const int fallbacks[] = {arch, 90, 89, 86, 80, 75, 70, 60, 52};
    bool built = false;
    for (int target : fallbacks) {
      if (target > arch) continue;
      nvrtcProgram prog = nullptr;
      if (rtc.CreateProgram(&prog, cuda_kernel_source, "postflop.cu", 0, nullptr, nullptr) != 0)
        throw std::runtime_error("NVRTC could not create the program");
      std::vector<std::string> options = {"--gpu-architecture=compute_" + std::to_string(target), "--fmad=false",
                                          "--prec-div=true", "--prec-sqrt=true"};
      if (variant != kFloatState) options.push_back("-DWSOLVER_BFLOAT=1");
      if (variant == kBfloatRegretsHalfSums) options.push_back("-DWSOLVER_FP16_SUMS=1");
      std::vector<const char*> argv;
      for (const auto& o : options) argv.push_back(o.c_str());
      const nvrtcResult result = rtc.CompileProgram(prog, int(argv.size()), argv.data());
      size_t n = 0;
      rtc.GetProgramLogSize(prog, &n);
      log.assign(n, '\0');
      if (n) rtc.GetProgramLog(prog, log.data());
      if (result == 0) {
        rtc.GetPTXSize(prog, &n);
        ptx.assign(n, '\0');
        rtc.GetPTX(prog, ptx.data());
        built = true;
      }
      rtc.DestroyProgram(&prog);
      if (built) break;
      // 6: NVRTC_ERROR_COMPILATION, which is a fault in the source, not the architecture... unless
      // the option named an architecture this NVRTC does not know (NVRTC_ERROR_INVALID_OPTION, 5)
      if (result != 5) break;
    }
    if (!built) throw std::runtime_error("CUDA kernels did not compile: " + log.substr(0, 4000));
    current();
    CUmodule m = nullptr;
    check(cu.ModuleLoadData(&m, ptx.c_str()), "module");
    return modules[variant] = m;
  }

  void* kernel(Variant variant, const char* name) override {
    current();
    auto& slot = functions[variant][name];
    if (slot) return slot;
    CUfunction f = nullptr;
    check(cu.ModuleGetFunction(&f, module(variant), name), name);
    int threads = 0, static_shared = 0;
    cu.FuncGetAttribute(&threads, CU_FUNC_ATTRIBUTE_MAX_THREADS_PER_BLOCK, f);
    cu.FuncGetAttribute(&static_shared, CU_FUNC_ATTRIBUTE_SHARED_SIZE_BYTES, f);
    size_t dynamic = shared_limit > size_t(static_shared) ? shared_limit - static_shared : 0;
    // past 48 KB a kernel must opt in to its dynamic shared memory
    if (dynamic + static_shared > 48 * 1024 &&
        cu.FuncSetAttribute(f, CU_FUNC_ATTRIBUTE_MAX_DYNAMIC_SHARED_SIZE_BYTES, int(dynamic)) != CUDA_SUCCESS)
      dynamic = 48 * 1024 > size_t(static_shared) ? 48 * 1024 - static_shared : 0;
    info_by_function[f] = {std::min<size_t>(size_t(threads), 256), dynamic};
    return slot = f;
  }
  size_t max_width(void* k) override { return info_by_function[static_cast<CUfunction>(k)].width; }
  size_t local_limit(void* k) override { return info_by_function[static_cast<CUfunction>(k)].local; }

  Buf alloc(size_t bytes) override {
    current();
    CUdeviceptr p = 0;
    check(cu.MemAlloc(&p, bytes), "allocation");
    return Buf(p);
  }
  void release(Buf b) override {
    current();
    cu.MemFree(CUdeviceptr(b));
  }
  void write(Buf b, size_t offset, const void* data, size_t bytes, bool blocking) override {
    if (!bytes) return;
    current();
    check(cu.MemcpyHtoDAsync(CUdeviceptr(b) + offset, data, bytes, stream), "write");
    if (blocking) finish();
  }
  void read(Buf b, size_t offset, void* data, size_t bytes, bool blocking) override {
    if (!bytes) return;
    current();
    check(cu.MemcpyDtoHAsync(data, CUdeviceptr(b) + offset, bytes, stream), "read");
    if (blocking) finish();
  }
  void zero(Buf b, size_t bytes) override {
    current();
    check(cu.MemsetD32Async(CUdeviceptr(b), 0, bytes / 4, stream), "fill");
  }
  void launch(void* kernel, const char* name, size_t groups_x, size_t groups_y, size_t width, size_t scratch,
              const std::vector<Arg>& args) override {
    current();
    std::vector<void*> params;
    params.reserve(args.size());
    for (const Arg& a : args) params.push_back(a.buffer ? static_cast<void*>(const_cast<Buf*>(&a.buf)) : static_cast<void*>(const_cast<uint8_t*>(a.bytes)));
    CUevent start = nullptr, end = nullptr;
    if (profile().on) {
      cu.EventCreate(&start, 0);
      cu.EventCreate(&end, 0);
      cu.EventRecord(start, stream);
    }
    check(cu.LaunchKernel(static_cast<CUfunction>(kernel), unsigned(groups_x), unsigned(groups_y), 1, unsigned(width), 1, 1,
                          unsigned((scratch + 15) & ~size_t(15)), stream, params.data(), nullptr),
          name);
    if (start) {
      cu.EventRecord(end, stream);
      cu.EventSynchronize(end);
      float ms = 0;
      cu.EventElapsedTime(&ms, start, end);
      profile().add(name, ms * 1e-3);
      cu.EventDestroy(start);
      cu.EventDestroy(end);
    }
  }
  void flush() override {}
  void finish() override {
    current();
    check(cu.StreamSynchronize(stream), "finish");
  }
};

}  // namespace

std::unique_ptr<Backend> make_cuda_backend(std::string& error) {
  auto b = std::make_unique<Cuda>();
  try {
    b->init();
  } catch (const std::exception& e) {
    error = e.what();
    return nullptr;
  }
  return b;
}

}  // namespace gpu
}  // namespace pio
#endif
