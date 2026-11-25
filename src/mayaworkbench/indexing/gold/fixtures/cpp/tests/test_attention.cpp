#include "../src/attention.cu"
int main() { float x[4] = {0}; attention_forward(x, 4); return 0; }
