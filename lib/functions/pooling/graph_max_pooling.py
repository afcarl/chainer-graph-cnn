#!/usr/bin/env python
# -*- coding: utf-8 -*-

import numpy as np

import chainer
from chainer import function
from chainer import cuda
from chainer.cuda import cupy

if chainer.cuda.available:
    # x will be flattened in C-order
    # y will be flattened in C-order
    gpu_graphpool_fwd = cupy.ElementwiseKernel(
            'I p, I p_dim, raw I pooling_inds, raw T x',
            'T y, I max_ind',
            '''
            int n_cols = _ind.size() / p;
            int row_idx = i / n_cols;
            int col_idx = i % n_cols;
            int idx0 = pooling_inds[row_idx * p_dim + 0];
            int idx1 = pooling_inds[row_idx * p_dim + 1];
            T x0 = x[idx0 * n_cols + col_idx];
            T x1 = x[idx1 * n_cols + col_idx];
            y = max(x0, x1);
            max_ind = x0 > x1 ? idx0 : idx1;
            ''',
            'gpu_graphpool_fwd'
            )

    gpu_graphpool_bwd = cupy.ElementwiseKernel(
            'I p, I q, raw I max_inds, raw T gy',
            'T gx',
            '''
            int n_cols = _ind.size() / p;
            int row_idx = i / n_cols;
            int col_idx = i % n_cols;
            T val = 0;
            for (int j=0; j < q; j++) {
                int offset = j * n_cols + col_idx;
                if (max_inds[offset] == row_idx) {
                    val = gy[offset];
                    break;
                }
            }
            gx = val;

            ''',
            'gpu_graphpool_bwd'
            )


class GraphMaxPoolingFunction(function.Function):

    def __init__(self, pooling_inds):
        self.pooling_inds = np.array(pooling_inds).astype(np.int32)

    def forward_cpu(self, inputs):
        x = inputs[0]
        n_batch, c, N = x.shape
        # x.shape = (n_batch, c, N)
        x_pairs = x[:, :, self.pooling_inds]
        # x_pairs = (n_batch*c*N_coarse, 2)
        N_coarse = len(self.pooling_inds)
        m = self.pooling_inds[np.arange(N_coarse), x_pairs.argmax(axis=3)]
        x_inds = np.arange(x.size).reshape(x.shape)
        self.max_inds = x_inds[
               np.arange(n_batch)[:, None, None],
               np.arange(c)[None, :, None],
               m]
        # max_inds.shape = (n_batch, c, N_coarse)
        return x_pairs.max(axis=3),

    def forward_gpu(self, inputs):
        x = inputs[0]
        xp = cuda.get_array_module(x)
        n_batch, c, N = x.shape
        N_coarse = len(self.pooling_inds)
        # x.shape = (n_batch, c, N)
        x = x.transpose((2, 1, 0))
        # x.shape = (N, c, n_batch)
        p_dim = self.pooling_inds.shape[1]
        with cuda.get_device(x.data):
            y = xp.empty((N_coarse, c, n_batch), dtype=x.dtype)
            self.max_inds = xp.empty((N_coarse, c, n_batch), dtype=np.int32)
            pooling_inds = cuda.to_gpu(self.pooling_inds)
            gpu_graphpool_fwd(N_coarse, p_dim, pooling_inds, x, y, self.max_inds)
        y = y.transpose((2, 1, 0))
        # y.shape = (n_batch, c, N_coarse)

        return y,

    def backward_cpu(self, inputs, grad_outputs):
        x = inputs[0]
        n_batch, c_in, N = x.shape
        # x.shape = (n_batch, c_in, N)
        gy = grad_outputs[0]
        # gy.shape = (n_batch, c_in, N_coarse)
        gx = np.zeros((n_batch*c_in*N), dtype=x.dtype)
        inds = self.max_inds.ravel()
        gx[inds] = gy.ravel()
        gx = gx.reshape(x.shape)
        return gx,

    def backward_gpu(self, inputs, grad_outputs):
        x = inputs[0]
        xp = cuda.get_array_module(x)
        n_batch, c_in, N = x.shape
        # x.shape = (n_batch, c_in, N)
        x = x.transpose((2, 1, 0))
        # x.shape = (N, c, n_batch)
        gy = grad_outputs[0]
        N_coarse = gy.shape[2]
        gy = gy.transpose((2, 1, 0))
        # gy.shape = (n_batch, c_in, N_coarse)
        with cuda.get_device(x.data):
            gx = xp.zeros((N, c_in, n_batch), dtype=x.dtype)
            gpu_graphpool_bwd(N, N_coarse, self.max_inds, gy, gx)
        gx = gx.transpose((2, 1, 0))
        # gx.shape = (n_batch, c_in, N)
        return gx,


def graph_max_pooling(x, pooling_inds):
    return GraphMaxPoolingFunction(pooling_inds)(x)
