#include "parallel.hpp"

#include <algorithm>
#include <cstring>

namespace pio {

ThreadPool::ThreadPool(int threads) { set_threads(threads); }

ThreadPool::~ThreadPool() { stop_all(); }

void ThreadPool::set_threads(int threads) {
  if (threads <= 0) threads = static_cast<int>(std::max(1u, std::thread::hardware_concurrency()));
  if (threads == nthreads_ && static_cast<int>(workers_.size()) == threads - 1) return;
  stop_all();
  nthreads_ = threads;
  start(threads - 1);
}

void ThreadPool::start(int workers) {
  quit_ = false;
  for (int i = 0; i < workers; ++i) workers_.emplace_back([this] { worker_loop(); });
}

void ThreadPool::stop_all() {
  {
    std::lock_guard<std::mutex> lk(m_);
    quit_ = true;
  }
  cv_.notify_all();
  for (auto& t : workers_) t.join();
  workers_.clear();
}

// Caller must hold m_. Picks the topmost job (index >= min_index) with work left.
ThreadPool::Job* ThreadPool::pick_job(size_t min_index) {
  for (size_t i = jobs_.size(); i-- > min_index;) {
    Job* j = jobs_[i];
    if (j->next.load(std::memory_order_relaxed) < j->n) {
      j->active.fetch_add(1, std::memory_order_acq_rel);
      return j;
    }
  }
  return nullptr;
}

bool ThreadPool::run_some(Job* j) {
  bool did = false;
  for (;;) {
    int i = j->next.fetch_add(1, std::memory_order_acq_rel);
    if (i >= j->n) break;
    (*j->fn)(i);
    j->done.fetch_add(1, std::memory_order_acq_rel);
    did = true;
  }
  j->active.fetch_sub(1, std::memory_order_acq_rel);
  return did;
}

void ThreadPool::worker_loop() {
  for (;;) {
    Job* j = nullptr;
    {
      std::unique_lock<std::mutex> lk(m_);
      cv_.wait(lk, [&] {
        if (quit_) return true;
        j = pick_job(0);
        return j != nullptr;
      });
      if (quit_ && !j) return;
    }
    run_some(j);
  }
}

void ThreadPool::parallel_for(int n, const std::function<void(int)>& f) {
  if (n <= 0) return;
  if (n == 1 || nthreads_ <= 1) {
    for (int i = 0; i < n; ++i) f(i);
    return;
  }
  Job job;
  job.fn = &f;
  job.n = n;
  size_t my_index;
  {
    std::lock_guard<std::mutex> lk(m_);
    my_index = jobs_.size();
    jobs_.push_back(&job);
    job.active.fetch_add(1, std::memory_order_acq_rel);  // the caller
  }
  cv_.notify_all();
  run_some(&job);
  // Wait for stragglers; help with jobs nested above ours meanwhile.
  while (job.done.load(std::memory_order_acquire) < n) {
    Job* other = nullptr;
    {
      std::lock_guard<std::mutex> lk(m_);
      // Prefer work nested above our job; otherwise help any job (work stealing). Tasks always
      // finish without blocking on us, so this cannot deadlock.
      other = pick_job(my_index + 1);
      if (!other) other = pick_job(0);
    }
    if (other) run_some(other);
    else std::this_thread::yield();
  }
  {
    std::lock_guard<std::mutex> lk(m_);
    auto it = std::find(jobs_.begin(), jobs_.end(), &job);
    if (it != jobs_.end()) jobs_.erase(it);
  }
  while (job.active.load(std::memory_order_acquire) > 0) std::this_thread::yield();
}

ThreadPool& pool() {
  static ThreadPool p(0);
  return p;
}

float* Arena::alloc(size_t n) {
  n = (n + 15) & ~size_t(15);
  for (;;) {
    if (cur_ < blocks_.size()) {
      Block& b = blocks_[cur_];
      if (off_ + n <= b.size) {
        float* p = b.mem.get() + off_;
        off_ += n;
        return p;
      }
      ++cur_;
      off_ = 0;
      continue;
    }
    size_t sz = std::max(kBlockFloats, n);
    Block b;
    // over-allocate for 64-byte alignment
    b.mem.reset(new float[sz + 16]);
    b.size = sz;
    blocks_.push_back(std::move(b));
    // Align start of the block.
    // (new[] for float is at least 16-byte aligned; we keep offsets multiple of 16 floats.)
  }
}

float* Arena::alloc_zero(size_t n) {
  float* p = alloc(n);
  std::memset(p, 0, n * sizeof(float));
  return p;
}

Arena& arena() {
  thread_local Arena a;
  return a;
}

}  // namespace pio
