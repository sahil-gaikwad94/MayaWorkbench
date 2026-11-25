#include <cuda_runtime.h>

__global__ void softmax_kernel(float* x, int n) { /* stable softmax */ }
void attention_forward(float* x, int n) { softmax_kernel<<<1, n>>>(x, n); }
