# Buffer Optimization Implementation Guide

## Quick Reference

**Document:** Buffer Write Performance Optimization  
**Current Performance:** 27.08μs/write (36K ops/sec)  
**Target Performance:** <10μs/write (>100K ops/sec)  
**Recommended Solution:** Solution 2 (Balanced Approach)  
**Expected Result:** ~8μs/write (125K ops/sec)

---

## Implementation Roadmap

### 🎯 Phase 1: Quick Wins (Week 1)

**Goal:** Achieve <15μs per write  
**Effort:** 2-4 hours  
**Files to modify:**
- [`level2/buffers/ring_buffer.py`](../buffers/ring_buffer.py)
- [`level2/test_system.py`](../test_system.py)

#### Step 1.1: Add Timestamp Caching

**File:** `level2/buffers/ring_buffer.py`

```python
class SharedMemoryRingBuffer:
    def __init__(self, config: BufferConfig, create: bool = True):
        # ... existing code ...
        
        # NEW: Add timestamp caching
        self._cached_timestamp = 0
        self._timestamp_cache_time = 0.0
        self._timestamp_cache_interval = 0.0001  # 100μs
```

#### Step 1.2: Pre-compile msgpack Packer

```python
class SharedMemoryRingBuffer:
    def __init__(self, config: BufferConfig, create: bool = True):
        # ... existing code ...
        
        # NEW: Pre-compile packer
        if HAS_MSGPACK:
            self._packer = msgpack.Packer(use_bin_type=True)
        else:
            self._packer = None
```

#### Step 1.3: Optimize put_msgpack Method

```python
def put_msgpack(self, data: dict) -> bool:
    """Optimized version with caching"""
    # Use cached timestamp
    current_time = time.time()
    if current_time - self._timestamp_cache_time > self._timestamp_cache_interval:
        self._cached_timestamp = int(current_time * 1000000)
        self._timestamp_cache_time = current_time
    
    # Use pre-compiled packer
    if self._packer:
        packed = self._packer.pack(data)
    else:
        packed = json.dumps(data).encode('utf-8')
    
    return self._put_with_timestamp(packed, self._cached_timestamp)

def _put_with_timestamp(self, data: bytes, timestamp: int) -> bool:
    """Internal put with pre-computed timestamp"""
    if len(data) > self.slot_data_size:
        return False
    
    write_pos = self._get_write_pos()
    read_pos = self._get_read_pos()
    
    # Check if full
    next_pos = (write_pos + 1) % self.slot_count
    if next_pos == read_pos:
        self._increment_overflow()
        self._set_read_pos((read_pos + 1) % self.slot_count)
    
    slot_offset = self._get_slot_offset(write_pos)
    
    # Write slot (optimized)
    self._shm.buf[slot_offset] = 0  # Invalid during write
    struct.pack_into('Q', self._shm.buf, slot_offset + 1, timestamp)
    struct.pack_into('I', self._shm.buf, slot_offset + 9, len(data))
    
    data_offset = slot_offset + self.SLOT_HEADER_SIZE
    self._shm.buf[data_offset:data_offset + len(data)] = data
    
    self._shm.buf[slot_offset] = 1  # Valid
    self._set_write_pos(next_pos)
    
    return True
```

#### Step 1.4: Update Tests

Add new test to `level2/test_system.py`:

```python
def test_buffer_performance_optimized():
    """测试优化后的缓冲区性能"""
    print("=" * 70)
    print("测试1b: 优化后缓冲区写入性能")
    print("=" * 70)
    
    config = BufferConfig(name="test_perf_opt", slot_count=100000, slot_data_size=512)
    buffer = SharedMemoryRingBuffer(config, create=True)
    
    test_data = {
        'type': 'l2order',
        'stock_code': '600000.SH',
        'data': {
            'time': 1234567890000,
            'price': 10.5,
            'volume': 1000,
            'entrustNo': 12345,
            'entrustDirection': 1
        }
    }
    
    count = 10000
    start_time = time.time()
    
    for i in range(count):
        buffer.put_msgpack(test_data)
    
    elapsed = time.time() - start_time
    
    print(f"写入 {count} 条数据")
    print(f"总耗时: {elapsed*1000:.2f}ms")
    print(f"平均每次: {elapsed/count*1000000:.2f}μs")
    print(f"吞吐量: {count/elapsed:.0f} 条/秒")
    print(f"改进: {27.08 / (elapsed/count*1000000):.2f}x")
    
    avg_time_us = elapsed / count * 1000000
    if avg_time_us < 15:
        print("✅ Phase 1 目标达成 (< 15μs)")
    else:
        print(f"⚠️  未达标 ({avg_time_us:.2f}μs > 15μs)")
    
    buffer.unlink()
    print()
```

**Expected Results:**
- Write time: ~15μs (1.8x improvement)
- Throughput: ~67K ops/sec

---

### 🚀 Phase 2: Target Achievement (Week 2-3)

**Goal:** Achieve <10μs per write  
**Effort:** 1-2 days  
**Recommended Approach**

#### Step 2.1: Add Batch Write API

**File:** `level2/buffers/ring_buffer.py`

```python
class SharedMemoryRingBuffer:
    def __init__(self, config: BufferConfig, create: bool = True):
        # ... existing code from Phase 1 ...
        
        # NEW: Add write buffer for zero-copy operations
        self._write_buffer = bytearray(config.slot_data_size)
        self._mem_view = memoryview(self._shm.buf)
    
    def put_msgpack_batch(self, items: list, batch_timestamp: int = None) -> int:
        """
        Batch write multiple items with shared timestamp
        
        Args:
            items: List of data dictionaries to write
            batch_timestamp: Optional pre-computed timestamp (microseconds)
                           If None, will compute once for the batch
        
        Returns:
            Number of items successfully written
        """
        if batch_timestamp is None:
            batch_timestamp = int(time.time() * 1000000)
        
        success_count = 0
        for data in items:
            if self._packer:
                packed = self._packer.pack(data)
            else:
                packed = json.dumps(data).encode('utf-8')
            
            if self._put_with_timestamp(packed, batch_timestamp):
                success_count += 1
        
        return success_count
    
    def put_msgpack_fast(self, data: dict, timestamp: int) -> bool:
        """
        Fastest write path - caller provides timestamp
        
        Use this when you have pre-computed timestamp (e.g., from callback)
        """
        if self._packer:
            packed = self._packer.pack(data)
        else:
            packed = json.dumps(data).encode('utf-8')
        
        return self._put_with_timestamp(packed, timestamp)
```

#### Step 2.2: Optimize Buffer Manager Callbacks

**File:** `level2/buffers/ring_buffer.py`

```python
class Level2BufferManager:
    def on_l2order_callback(self, datas: dict):
        """
        Optimized l2order callback with batch processing
        
        Args:
            datas: {stock_code: order_dict}
        """
        # Compute timestamp once per callback
        batch_timestamp = int(time.time() * 1000000)
        
        # Build batch items
        items = []
        for stock_code, order_data in datas.items():
            items.append({
                'type': 'l2order',
                'stock_code': stock_code,
                'data': order_data
            })
        
        # Batch write
        self.l2order_buffer.put_msgpack_batch(items, batch_timestamp)
    
    def on_l2transaction_callback(self, datas: dict):
        """Optimized l2transaction callback"""
        batch_timestamp = int(time.time() * 1000000)
        
        items = []
        for stock_code, trans_data in datas.items():
            items.append({
                'type': 'l2transaction',
                'stock_code': stock_code,
                'data': trans_data
            })
        
        self.l2transaction_buffer.put_msgpack_batch(items, batch_timestamp)
    
    def on_l2quote_callback(self, datas: dict):
        """Optimized l2quote callback"""
        batch_timestamp = int(time.time() * 1000000)
        
        items = []
        for stock_code, quote_data in datas.items():
            items.append({
                'type': 'l2quote',
                'stock_code': stock_code,
                'data': quote_data
            })
        
        self.l2quote_buffer.put_msgpack_batch(items, batch_timestamp)
```

#### Step 2.3: Add Memoryview Optimization

```python
def _put_with_timestamp(self, data: bytes, timestamp: int) -> bool:
    """Optimized with memoryview for zero-copy writes"""
    if len(data) > self.slot_data_size:
        return False
    
    write_pos = self._get_write_pos()
    read_pos = self._get_read_pos()
    
    next_pos = (write_pos + 1) % self.slot_count
    if next_pos == read_pos:
        self._increment_overflow()
        self._set_read_pos((read_pos + 1) % self.slot_count)
    
    slot_offset = self._get_slot_offset(write_pos)
    
    # Use memoryview for zero-copy
    slot_view = self._mem_view[slot_offset:slot_offset + self.slot_size]
    
    slot_view[0] = 0  # Invalid during write
    slot_view[1:9] = timestamp.to_bytes(8, 'little')
    slot_view[9:13] = len(data).to_bytes(4, 'little')
    slot_view[13:13+len(data)] = data
    slot_view[0] = 1  # Valid
    
    self._set_write_pos(next_pos)
    return True
```

#### Step 2.4: Enhanced Testing

Add comprehensive performance tests:

```python
def test_batch_write_performance():
    """测试批量写入性能"""
    print("=" * 70)
    print("测试2: 批量写入性能")
    print("=" * 70)
    
    config = BufferConfig(name="test_batch", slot_count=100000, slot_data_size=512)
    buffer = SharedMemoryRingBuffer(config, create=True)
    
    # Prepare batch data
    batch_data = []
    for i in range(100):
        batch_data.append({
            'type': 'l2order',
            'stock_code': f'{600000+i:06d}.SH',
            'data': {
                'time': 1234567890000 + i,
                'price': 10.5,
                'volume': 1000,
                'entrustNo': 12345 + i,
                'entrustDirection': 1
            }
        })
    
    # Test batch write
    iterations = 100
    start_time = time.time()
    
    for _ in range(iterations):
        batch_timestamp = int(time.time() * 1000000)
        buffer.put_msgpack_batch(batch_data, batch_timestamp)
    
    elapsed = time.time() - start_time
    total_writes = iterations * len(batch_data)
    
    print(f"批量大小: {len(batch_data)}")
    print(f"批次数: {iterations}")
    print(f"总写入: {total_writes} 条")
    print(f"总耗时: {elapsed*1000:.2f}ms")
    print(f"平均每次: {elapsed/total_writes*1000000:.2f}μs")
    print(f"吞吐量: {total_writes/elapsed:.0f} 条/秒")
    
    avg_time_us = elapsed / total_writes * 1000000
    if avg_time_us < 10:
        print("✅ Phase 2 目标达成 (< 10μs)")
    else:
        print(f"⚠️  接近目标 ({avg_time_us:.2f}μs)")
    
    buffer.unlink()
    print()
```

**Expected Results:**
- Single write time: ~8μs (3.4x improvement)
- Batch write time: ~5-6μs per item
- Throughput: ~125K ops/sec

---

## Migration Strategy

### Backward Compatibility

The optimized implementation maintains backward compatibility:

```python
# Old code still works
buffer.put_msgpack(data)  # Uses cached timestamp internally

# New optimized code
items = [data1, data2, data3]
buffer.put_msgpack_batch(items, batch_timestamp)
```

### Gradual Rollout

1. **Week 1:** Deploy Phase 1 to development environment
2. **Week 2:** Monitor performance, validate results
3. **Week 3:** Deploy Phase 2 to staging
4. **Week 4:** Production rollout with feature flag

### Feature Flag Implementation

```python
class BufferConfig:
    def __init__(self, name: str, slot_count: int, slot_data_size: int,
                 enable_optimizations: bool = True):
        self.name = name
        self.slot_count = slot_count
        self.slot_data_size = slot_data_size
        self.enable_optimizations = enable_optimizations

class SharedMemoryRingBuffer:
    def put_msgpack(self, data: dict) -> bool:
        if self.config.enable_optimizations:
            return self._put_msgpack_optimized(data)
        else:
            return self._put_msgpack_legacy(data)
```

---

## Testing Checklist

### Unit Tests

- [ ] Test timestamp caching accuracy
- [ ] Test pre-compiled packer functionality
- [ ] Test batch write correctness
- [ ] Test memoryview operations
- [ ] Test overflow handling
- [ ] Test concurrent read/write

### Performance Tests

- [ ] Benchmark single write performance
- [ ] Benchmark batch write performance
- [ ] Benchmark peak load (9:25 scenario)
- [ ] Memory usage profiling
- [ ] CPU usage profiling

### Integration Tests

- [ ] Test with real L2 data
- [ ] Test with multiple consumer processes
- [ ] Test with different buffer sizes
- [ ] Test error recovery
- [ ] Test data integrity across process boundaries

### Regression Tests

- [ ] Verify all existing tests pass
- [ ] Verify no data loss
- [ ] Verify timestamp accuracy
- [ ] Verify msgpack compatibility

---

## Monitoring & Validation

### Key Metrics to Track

```python
class BufferMetrics:
    """Metrics for monitoring buffer performance"""
    
    def __init__(self):
        self.write_count = 0
        self.write_time_total = 0.0
        self.overflow_count = 0
        self.batch_count = 0
        self.batch_size_total = 0
    
    def record_write(self, elapsed_time: float):
        self.write_count += 1
        self.write_time_total += elapsed_time
    
    def record_batch(self, batch_size: int, elapsed_time: float):
        self.batch_count += 1
        self.batch_size_total += batch_size
        self.write_time_total += elapsed_time
    
    def get_stats(self) -> dict:
        return {
            'total_writes': self.write_count + self.batch_size_total,
            'avg_write_time_us': (self.write_time_total / 
                                 (self.write_count + self.batch_size_total) 
                                 * 1000000),
            'avg_batch_size': (self.batch_size_total / self.batch_count 
                              if self.batch_count > 0 else 0),
            'overflow_count': self.overflow_count
        }
```

### Performance Dashboard

Create a simple dashboard to monitor:
- Average write time (μs)
- P50, P95, P99 latency
- Throughput (ops/sec)
- Buffer utilization
- Overflow events

---

## Troubleshooting Guide

### Issue: Performance not improving

**Symptoms:** Write time still >15μs after Phase 1

**Diagnosis:**
1. Check if msgpack is actually installed: `python -c "import msgpack; print(msgpack.version)"`
2. Verify packer is being used: Add debug logging
3. Check timestamp caching: Print cache hit rate

**Solutions:**
- Install/upgrade msgpack: `pip install -U msgpack`
- Verify no JSON fallback is occurring
- Check cache interval settings

### Issue: Data corruption

**Symptoms:** Consumer reads incorrect data

**Diagnosis:**
1. Check valid flag handling
2. Verify memoryview boundaries
3. Check concurrent access patterns

**Solutions:**
- Add memory barriers if needed
- Increase slot_data_size if truncation
- Review synchronization logic

### Issue: Memory usage increased

**Symptoms:** Higher memory consumption

**Diagnosis:**
1. Check for leaked memoryviews
2. Profile memory allocation
3. Check buffer count

**Solutions:**
- Ensure proper cleanup
- Review object lifecycle
- Consider buffer pooling

---

## Success Criteria

### Phase 1 Success

- ✅ Average write time <15μs
- ✅ All existing tests pass
- ✅ No data corruption
- ✅ No memory leaks

### Phase 2 Success

- ✅ Average write time <10μs
- ✅ Batch operations working correctly
- ✅ 9:25 peak load handled successfully
- ✅ Production stability maintained

### Overall Success

- ✅ 2.7x+ performance improvement
- ✅ Backward compatible
- ✅ Code maintainability preserved
- ✅ Documentation complete
- ✅ Team trained on new APIs

---

## Support & Resources

### Documentation
- Main optimization plan: [`buffer_optimization_plan.md`](buffer_optimization_plan.md)
- Architecture: [`ARCHITECTURE.md`](../ARCHITECTURE.md)
- Current implementation: [`ring_buffer.py`](../buffers/ring_buffer.py)

### Tools
- Performance profiling: `cProfile`, `py-spy`
- Memory profiling: `memory_profiler`, `tracemalloc`
- Testing: `pytest`, `pytest-benchmark`

### References
- msgpack documentation: https://msgpack.org/
- Python multiprocessing: https://docs.python.org/3/library/multiprocessing.html
- Shared memory: https://docs.python.org/3/library/multiprocessing.shared_memory.html
