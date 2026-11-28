// The OpenCL backend of the discrete-GPU solve (gpu_backend.hpp): AMD Radeon, Intel, and NVIDIA
// through its OpenCL driver when CUDA is not there. OpenCL is loaded at run time (OpenCL.dll,
// libOpenCL.so.1), so the solver needs no SDK to build and still starts on a machine without a GPU
// driver. The kernels are OpenCL C 1.2 (gpu_opencl.h), compiled by the driver on first use.
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
#define WSOLVER_CL_API __stdcall
#else
#include <dlfcn.h>
#define WSOLVER_CL_API
#endif

#include <algorithm>
#include <cstring>
#include <new>
#include <stdexcept>

#include "gpu_backend.hpp"

namespace pio {
namespace gpu {
namespace {

// ----- the OpenCL C API, as much of it as is used (the ABI has been fixed since 1.0) ----------------
typedef int32_t cl_int;
typedef uint32_t cl_uint;
typedef uint64_t cl_ulong;
typedef cl_ulong cl_bitfield;
typedef cl_uint cl_bool;
typedef struct _cl_platform_id* cl_platform_id;
typedef struct _cl_device_id* cl_device_id;
typedef struct _cl_context* cl_context;
typedef struct _cl_command_queue* cl_command_queue;
typedef struct _cl_mem* cl_mem;
typedef struct _cl_program* cl_program;
typedef struct _cl_kernel* cl_kernel;
typedef struct _cl_event* cl_event;
typedef intptr_t cl_context_properties;

constexpr cl_int CL_SUCCESS = 0;
constexpr cl_bitfield CL_DEVICE_TYPE_GPU = 1 << 2;
constexpr cl_uint CL_DEVICE_MAX_COMPUTE_UNITS = 0x1002, CL_DEVICE_MAX_CLOCK_FREQUENCY = 0x100C,
                  CL_DEVICE_MAX_MEM_ALLOC_SIZE = 0x1010, CL_DEVICE_SINGLE_FP_CONFIG = 0x101B,
                  CL_DEVICE_GLOBAL_MEM_SIZE = 0x101F, CL_DEVICE_LOCAL_MEM_SIZE = 0x1023,
                  CL_DEVICE_AVAILABLE = 0x1027, CL_DEVICE_NAME = 0x102B, CL_DEVICE_HOST_UNIFIED_MEMORY = 0x1035,
                  CL_DEVICE_OPENCL_C_VERSION = 0x103D, CL_DEVICE_BOARD_NAME_AMD = 0x4038,
                  CL_PROGRAM_BUILD_LOG = 0x1183, CL_KERNEL_WORK_GROUP_SIZE = 0x11B0,
                  CL_KERNEL_LOCAL_MEM_SIZE = 0x11B2;
constexpr cl_bitfield CL_FP_CORRECTLY_ROUNDED_DIVIDE_SQRT = 1 << 7;
constexpr cl_bitfield CL_MEM_READ_WRITE = 1 << 0;
constexpr cl_bitfield CL_QUEUE_PROFILING_ENABLE = 1 << 1;
constexpr cl_uint CL_PROFILING_COMMAND_START = 0x1282, CL_PROFILING_COMMAND_END = 0x1283;

struct Api {
  cl_int(WSOLVER_CL_API* GetPlatformIDs)(cl_uint, cl_platform_id*, cl_uint*);
  cl_int(WSOLVER_CL_API* GetDeviceIDs)(cl_platform_id, cl_bitfield, cl_uint, cl_device_id*, cl_uint*);
  cl_int(WSOLVER_CL_API* GetDeviceInfo)(cl_device_id, cl_uint, size_t, void*, size_t*);
  cl_context(WSOLVER_CL_API* CreateContext)(const cl_context_properties*, cl_uint, const cl_device_id*,
                                            void(WSOLVER_CL_API*)(const char*, const void*, size_t, void*), void*,
                                            cl_int*);
  cl_command_queue(WSOLVER_CL_API* CreateCommandQueue)(cl_context, cl_device_id, cl_bitfield, cl_int*);
  cl_program(WSOLVER_CL_API* CreateProgramWithSource)(cl_context, cl_uint, const char**, const size_t*, cl_int*);
  cl_int(WSOLVER_CL_API* BuildProgram)(cl_program, cl_uint, const cl_device_id*, const char*,
                                       void(WSOLVER_CL_API*)(cl_program, void*), void*);
  cl_int(WSOLVER_CL_API* GetProgramBuildInfo)(cl_program, cl_device_id, cl_uint, size_t, void*, size_t*);
  cl_kernel(WSOLVER_CL_API* CreateKernel)(cl_program, const char*, cl_int*);
  cl_int(WSOLVER_CL_API* GetKernelWorkGroupInfo)(cl_kernel, cl_device_id, cl_uint, size_t, void*, size_t*);
  cl_int(WSOLVER_CL_API* SetKernelArg)(cl_kernel, cl_uint, size_t, const void*);
  cl_mem(WSOLVER_CL_API* CreateBuffer)(cl_context, cl_bitfield, size_t, void*, cl_int*);
  cl_int(WSOLVER_CL_API* EnqueueWriteBuffer)(cl_command_queue, cl_mem, cl_bool, size_t, size_t, const void*, cl_uint,
                                             const cl_event*, cl_event*);
  cl_int(WSOLVER_CL_API* EnqueueReadBuffer)(cl_command_queue, cl_mem, cl_bool, size_t, size_t, void*, cl_uint,
                                            const cl_event*, cl_event*);
  cl_int(WSOLVER_CL_API* EnqueueFillBuffer)(cl_command_queue, cl_mem, const void*, size_t, size_t, size_t, cl_uint,
                                            const cl_event*, cl_event*);
  cl_int(WSOLVER_CL_API* EnqueueNDRangeKernel)(cl_command_queue, cl_kernel, cl_uint, const size_t*, const size_t*,
                                               const size_t*, cl_uint, const cl_event*, cl_event*);
  cl_int(WSOLVER_CL_API* Flush)(cl_command_queue);
  cl_int(WSOLVER_CL_API* Finish)(cl_command_queue);
  cl_int(WSOLVER_CL_API* ReleaseMemObject)(cl_mem);
  cl_int(WSOLVER_CL_API* GetEventProfilingInfo)(cl_event, cl_uint, size_t, void*, size_t*);
  cl_int(WSOLVER_CL_API* ReleaseEvent)(cl_event);

  bool load() {
#if defined(_WIN32)
    HMODULE lib = LoadLibraryA("OpenCL.dll");
    auto sym = [&](const char* name) { return reinterpret_cast<void*>(GetProcAddress(lib, name)); };
#else
    void* lib = dlopen("libOpenCL.so.1", RTLD_NOW | RTLD_LOCAL);
    if (!lib) lib = dlopen("libOpenCL.so", RTLD_NOW | RTLD_LOCAL);
    auto sym = [&](const char* name) { return dlsym(lib, name); };
#endif
    if (!lib) return false;
    bool ok = true;
    auto get = [&](auto& fn, const char* name) {
      void* p = sym(name);
      std::memcpy(&fn, &p, sizeof p);
      ok = ok && p;
    };
    get(GetPlatformIDs, "clGetPlatformIDs");
    get(GetDeviceIDs, "clGetDeviceIDs");
    get(GetDeviceInfo, "clGetDeviceInfo");
    get(CreateContext, "clCreateContext");
    get(CreateCommandQueue, "clCreateCommandQueue");
    get(CreateProgramWithSource, "clCreateProgramWithSource");
    get(BuildProgram, "clBuildProgram");
    get(GetProgramBuildInfo, "clGetProgramBuildInfo");
    get(CreateKernel, "clCreateKernel");
    get(GetKernelWorkGroupInfo, "clGetKernelWorkGroupInfo");
    get(SetKernelArg, "clSetKernelArg");
    get(CreateBuffer, "clCreateBuffer");
    get(EnqueueWriteBuffer, "clEnqueueWriteBuffer");
    get(EnqueueReadBuffer, "clEnqueueReadBuffer");
    get(EnqueueFillBuffer, "clEnqueueFillBuffer");
    get(EnqueueNDRangeKernel, "clEnqueueNDRangeKernel");
    get(Flush, "clFlush");
    get(Finish, "clFinish");
    get(ReleaseMemObject, "clReleaseMemObject");
    get(GetEventProfilingInfo, "clGetEventProfilingInfo");
    get(ReleaseEvent, "clReleaseEvent");
    return ok;
  }
};

void check(cl_int err, const char* what) {
  if (err == CL_SUCCESS) return;
  // -4 CL_MEM_OBJECT_ALLOCATION_FAILURE, -5 CL_OUT_OF_RESOURCES, -6 CL_OUT_OF_HOST_MEMORY
  if (err == -4 || err == -5 || err == -6) throw std::bad_alloc();
  throw std::runtime_error(std::string("OpenCL ") + what + " failed (" + std::to_string(err) + ")");
}

class OpenCL final : public Backend {
 public:
  Api cl{};
  cl_device_id dev = nullptr;
  cl_context ctx = nullptr;
  cl_command_queue queue = nullptr;
  cl_program programs[kVariants] = {};
  bool correct_division = false;
  uint64_t local_mem = 0;
  std::map<std::string, cl_kernel> kernels[kVariants];
  struct Info {
    size_t width, local;
  };
  std::map<cl_kernel, Info> info_by_kernel;

  const char* api() const override { return "OpenCL"; }

  template <class T>
  T info(cl_device_id d, cl_uint what) const {
    T value{};
    cl.GetDeviceInfo(d, what, sizeof value, &value, nullptr);
    return value;
  }
  std::string text(cl_device_id d, cl_uint what) const {
    size_t n = 0;
    if (cl.GetDeviceInfo(d, what, 0, nullptr, &n) != CL_SUCCESS || !n) return {};
    std::string s(n, '\0');
    cl.GetDeviceInfo(d, what, n, s.data(), nullptr);
    while (!s.empty() && (s.back() == '\0' || s.back() == ' ')) s.pop_back();
    return s;
  }

  void init() {
    if (!cl.load()) throw std::runtime_error("no OpenCL driver");
    cl_uint platforms = 0;
    if (cl.GetPlatformIDs(0, nullptr, &platforms) != CL_SUCCESS || !platforms) throw std::runtime_error("no OpenCL platform");
    std::vector<cl_platform_id> ids(platforms);
    cl.GetPlatformIDs(platforms, ids.data(), nullptr);
    // The GPU most likely to be the fast one: a discrete card over an integrated one, then the most
    // compute. OpenCL C 1.2 is enough; the river passes need 32 KB of work-group memory.
    double best = 0;
    for (auto platform : ids) {
      cl_uint n = 0;
      if (cl.GetDeviceIDs(platform, CL_DEVICE_TYPE_GPU, 0, nullptr, &n) != CL_SUCCESS || !n) continue;
      std::vector<cl_device_id> devices(n);
      cl.GetDeviceIDs(platform, CL_DEVICE_TYPE_GPU, n, devices.data(), nullptr);
      for (auto d : devices) {
        const std::string version = text(d, CL_DEVICE_OPENCL_C_VERSION);  // "OpenCL C 1.2 ..."
        const int major = version.size() > 9 ? std::atoi(version.c_str() + 9) : 0;
        const int minor = version.size() > 11 ? std::atoi(version.c_str() + 11) : 0;
        if (major * 10 + minor < 12 || !info<cl_bool>(d, CL_DEVICE_AVAILABLE) ||
            info<cl_ulong>(d, CL_DEVICE_LOCAL_MEM_SIZE) < 32 * 1024)
          continue;
        const double score = double(info<cl_uint>(d, CL_DEVICE_MAX_COMPUTE_UNITS)) *
                             std::max<cl_uint>(1, info<cl_uint>(d, CL_DEVICE_MAX_CLOCK_FREQUENCY)) *
                             (info<cl_bool>(d, CL_DEVICE_HOST_UNIFIED_MEMORY) ? 1 : 1000);
        if (score > best) {
          best = score;
          dev = d;
        }
      }
    }
    if (!dev) throw std::runtime_error("no OpenCL 1.2 GPU with 32 KB of work-group memory");
    device_name = text(dev, CL_DEVICE_BOARD_NAME_AMD);
    if (device_name.empty()) device_name = text(dev, CL_DEVICE_NAME);
    memory = info<cl_ulong>(dev, CL_DEVICE_GLOBAL_MEM_SIZE);
    max_alloc = info<cl_ulong>(dev, CL_DEVICE_MAX_MEM_ALLOC_SIZE);
    local_mem = info<cl_ulong>(dev, CL_DEVICE_LOCAL_MEM_SIZE);
    correct_division = info<cl_bitfield>(dev, CL_DEVICE_SINGLE_FP_CONFIG) & CL_FP_CORRECTLY_ROUNDED_DIVIDE_SQRT;
    cl_int err = 0;
    ctx = cl.CreateContext(nullptr, 1, &dev, nullptr, nullptr, &err);
    check(err, "context");
    queue = cl.CreateCommandQueue(ctx, dev, profile().on ? CL_QUEUE_PROFILING_ENABLE : 0, &err);
    check(err, "queue");
    program(kFloatState);  // a driver that cannot build the kernels offers no GPU
  }

  cl_program program(Variant variant) {
    if (programs[variant]) return programs[variant];
    ProfileScope ps(*this, "setup: compile kernels");
    cl_int err = 0;
    const char* source = opencl_kernel_source;
    cl_program p = cl.CreateProgramWithSource(ctx, 1, &source, nullptr, &err);
    check(err, "program");
    // IEEE division and square root like the CPU (OpenCL's default allows 2.5 ulp); no fast math.
    std::string options = "-cl-std=CL1.2";
    if (correct_division) options += " -cl-fp32-correctly-rounded-divide-sqrt";
    if (variant == kBfloatRegrets || variant == kBfloatRegretsHalfSums) options += " -D WSOLVER_BFLOAT=1";
    if (variant == kBfloatRegretsHalfSums || variant == kFloatRegretsHalfSums) options += " -D WSOLVER_FP16_SUMS=1";
    if (cl.BuildProgram(p, 1, &dev, options.c_str(), nullptr, nullptr) != CL_SUCCESS) {
      size_t n = 0;
      cl.GetProgramBuildInfo(p, dev, CL_PROGRAM_BUILD_LOG, 0, nullptr, &n);
      std::string log(n, '\0');
      cl.GetProgramBuildInfo(p, dev, CL_PROGRAM_BUILD_LOG, n, log.data(), nullptr);
      throw std::runtime_error("OpenCL kernels did not compile: " + log.substr(0, 4000));
    }
    return programs[variant] = p;
  }

  void* kernel(Variant variant, const char* name) override {
    auto& slot = kernels[variant][name];
    if (slot) return slot;
    cl_int err = 0;
    cl_kernel k = cl.CreateKernel(program(variant), name, &err);
    check(err, name);
    size_t w = 0;
    cl_ulong used = 0;
    cl.GetKernelWorkGroupInfo(k, dev, CL_KERNEL_WORK_GROUP_SIZE, sizeof w, &w, nullptr);
    cl.GetKernelWorkGroupInfo(k, dev, CL_KERNEL_LOCAL_MEM_SIZE, sizeof used, &used, nullptr);
    info_by_kernel[k] = {std::min<size_t>(w, 256), used < local_mem ? size_t(local_mem - used) : 0};
    return slot = k;
  }
  size_t max_width(void* k) override { return info_by_kernel[static_cast<cl_kernel>(k)].width; }
  size_t local_limit(void* k) override { return info_by_kernel[static_cast<cl_kernel>(k)].local; }

  Buf alloc(size_t bytes) override {
    cl_int err = 0;
    cl_mem m = cl.CreateBuffer(ctx, CL_MEM_READ_WRITE, bytes, nullptr, &err);
    check(err, "buffer");
    return reinterpret_cast<Buf>(m);
  }
  void release(Buf b) override { cl.ReleaseMemObject(reinterpret_cast<cl_mem>(b)); }
  void write(Buf b, size_t offset, const void* data, size_t bytes, bool blocking) override {
    if (bytes)
      check(cl.EnqueueWriteBuffer(queue, reinterpret_cast<cl_mem>(b), blocking, offset, bytes, data, 0, nullptr, nullptr),
            "write");
  }
  void read(Buf b, size_t offset, void* data, size_t bytes, bool blocking) override {
    if (bytes)
      check(cl.EnqueueReadBuffer(queue, reinterpret_cast<cl_mem>(b), blocking, offset, bytes, data, 0, nullptr, nullptr),
            "read");
  }
  void zero(Buf b, size_t bytes) override {
    const uint32_t z = 0;
    check(cl.EnqueueFillBuffer(queue, reinterpret_cast<cl_mem>(b), &z, sizeof z, 0, bytes & ~size_t(3), 0, nullptr,
                               nullptr),
          "fill");
  }
  void launch(void* kernel, const char* name, size_t groups_x, size_t groups_y, size_t width, size_t scratch,
              const std::vector<Arg>& args) override {
    cl_kernel k = static_cast<cl_kernel>(kernel);
    cl_uint i = 0;
    for (const Arg& a : args) {
      if (a.buffer) {
        cl_mem m = reinterpret_cast<cl_mem>(a.buf);
        check(cl.SetKernelArg(k, i++, sizeof m, &m), "argument");
      } else {
        check(cl.SetKernelArg(k, i++, a.size, a.bytes), "argument");
      }
    }
    if (scratch) check(cl.SetKernelArg(k, i, (scratch + 15) & ~size_t(15), nullptr), "work-group memory");
    const size_t local[2] = {width, 1};
    const size_t global[2] = {groups_x * width, groups_y};
    cl_event event = nullptr;
    check(cl.EnqueueNDRangeKernel(queue, k, groups_y > 1 ? 2 : 1, nullptr, global, local, 0, nullptr,
                                  profile().on ? &event : nullptr),
          name);
    if (event) {
      check(cl.Finish(queue), name);
      cl_ulong start = 0, end = 0;
      cl.GetEventProfilingInfo(event, CL_PROFILING_COMMAND_START, sizeof start, &start, nullptr);
      cl.GetEventProfilingInfo(event, CL_PROFILING_COMMAND_END, sizeof end, &end, nullptr);
      profile().add(name, (end - start) * 1e-9);
      cl.ReleaseEvent(event);
    }
  }
  void flush() override { check(cl.Flush(queue), "flush"); }
  void finish() override { check(cl.Finish(queue), "finish"); }
};

}  // namespace

// Kept for the life of the process (never released: at exit the driver may be gone first).
std::unique_ptr<Backend> make_opencl_backend(std::string& error) {
  auto b = std::make_unique<OpenCL>();
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
