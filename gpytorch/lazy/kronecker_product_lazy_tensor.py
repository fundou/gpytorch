#!/usr/bin/env python3

import operator
from functools import reduce

import torch

from ..utils.broadcasting import _matmul_broadcast_shape, _mul_broadcast_shape
from ..utils.memoize import cached
from .lazy_tensor import LazyTensor
from .non_lazy_tensor import lazify


def _prod(iterable):
    return reduce(operator.mul, iterable, 1)


def _matmul(lazy_tensors, kp_shape, rhs):
    output_shape = _matmul_broadcast_shape(kp_shape, rhs.shape)
    output_batch_shape = output_shape[:-2]

    res = rhs.contiguous().expand(*output_batch_shape, *rhs.shape[-2:])
    num_cols = rhs.size(-1)
    for lazy_tensor in lazy_tensors:
        res = res.view(*output_batch_shape, lazy_tensor.size(-1), -1)
        factor = lazy_tensor._matmul(res)
        factor = factor.view(*output_batch_shape, lazy_tensor.size(-2), -1, num_cols).transpose(-3, -2)
        res = factor.reshape(*output_batch_shape, -1, num_cols)
    return res


def _t_matmul(lazy_tensors, kp_shape, rhs):
    kp_t_shape = (*kp_shape[:-2], kp_shape[-1], kp_shape[-2])
    output_shape = _matmul_broadcast_shape(kp_t_shape, rhs.shape)
    output_batch_shape = torch.Size(output_shape[:-2])

    res = rhs.contiguous().expand(*output_batch_shape, *rhs.shape[-2:])
    num_cols = rhs.size(-1)
    for lazy_tensor in lazy_tensors:
        res = res.view(*output_batch_shape, lazy_tensor.size(-2), -1)
        factor = lazy_tensor._t_matmul(res)
        factor = factor.view(*output_batch_shape, lazy_tensor.size(-1), -1, num_cols).transpose(-3, -2)
        res = factor.reshape(*output_batch_shape, -1, num_cols)
    return res


class KroneckerProductLazyTensor(LazyTensor):
    def __init__(self, *lazy_tensors):
        try:
            lazy_tensors = tuple(lazify(lazy_tensor) for lazy_tensor in lazy_tensors)
        except TypeError:
            raise RuntimeError("KroneckerProductLazyTensor is intended to wrap lazy tensors.")
        for prev_lazy_tensor, curr_lazy_tensor in zip(lazy_tensors[:-1], lazy_tensors[1:]):
            if prev_lazy_tensor.batch_shape != curr_lazy_tensor.batch_shape:
                raise RuntimeError(
                    "KroneckerProductLazyTensor expects lazy tensors with the "
                    "same batch shapes. Got {}.".format([lv.batch_shape for lv in lazy_tensors])
                )
        super().__init__(*lazy_tensors)
        self.lazy_tensors = lazy_tensors

    @cached(name="cholesky")
    def _cholesky(self, upper=False):
        chol_factors = [lt._cholesky(upper=upper) for lt in self.lazy_tensors]
        return KroneckerProductTriangularLazyTensor(*chol_factors, upper=upper)

    def _get_indices(self, row_index, col_index, *batch_indices):
        row_factor = self.size(-2)
        col_factor = self.size(-1)

        res = None
        for lazy_tensor in self.lazy_tensors:
            sub_row_size = lazy_tensor.size(-2)
            sub_col_size = lazy_tensor.size(-1)

            row_factor //= sub_row_size
            col_factor //= sub_col_size
            sub_res = lazy_tensor._get_indices(
                (row_index // row_factor).fmod(sub_row_size),
                (col_index // col_factor).fmod(sub_col_size),
                *batch_indices,
            )
            res = sub_res if res is None else (sub_res * res)

        return res

    def _matmul(self, rhs):
        is_vec = rhs.ndimension() == 1
        if is_vec:
            rhs = rhs.unsqueeze(-1)

        res = _matmul(self.lazy_tensors, self.shape, rhs.contiguous())

        if is_vec:
            res = res.squeeze(-1)
        return res

    def _t_matmul(self, rhs):
        is_vec = rhs.ndimension() == 1
        if is_vec:
            rhs = rhs.unsqueeze(-1)

        res = _t_matmul(self.lazy_tensors, self.shape, rhs.contiguous())

        if is_vec:
            res = res.squeeze(-1)
        return res

    def _expand_batch(self, batch_shape):
        return self.__class__(*[lazy_tensor._expand_batch(batch_shape) for lazy_tensor in self.lazy_tensors])

    @cached(name="size")
    def _size(self):
        left_size = _prod(lazy_tensor.size(-2) for lazy_tensor in self.lazy_tensors)
        right_size = _prod(lazy_tensor.size(-1) for lazy_tensor in self.lazy_tensors)
        return torch.Size((*self.lazy_tensors[0].batch_shape, left_size, right_size))

    def _transpose_nonbatch(self):
        return self.__class__(*(lazy_tensor._transpose_nonbatch() for lazy_tensor in self.lazy_tensors), **self._kwargs)

    @cached
    def inverse(self):
        # here we use that (A \kron B)^-1 = A^-1 \kron B^-1
        inverses = [lt.inverse() for lt in self.lazy_tensors]
        return self.__class__(*inverses)

    def inv_matmul(self, right_tensor, left_tensor=None):
        # TODO: Investigate under what conditions computing individual individual inverses makes sense
        # For now, retain existing behavior
        return super().inv_matmul(right_tensor=right_tensor, left_tensor=left_tensor)

    def _inv_matmul(self, right_tensor, left_tensor=None):
        # Computes inv_matmul by exploiting the identity (A \kron B)^-1 = A^-1 \kron B^-1
        tsr_shapes = [q.size(-1) for q in self.lazy_tensors]
        n_rows = right_tensor.size(-2)
        batch_shape = _mul_broadcast_shape(self.shape[:-2], right_tensor.shape[:-2])
        perm_batch = tuple(range(len(batch_shape)))
        y = right_tensor.clone().expand(*batch_shape, *right_tensor.shape[-2:])
        for n, q in zip(tsr_shapes, self.lazy_tensors):
            # for KroneckerProductTriangularLazyTensor this inv_matmul is very cheap
            y = q.inv_matmul(y.reshape(*batch_shape, n, -1))
            y = y.reshape(*batch_shape, n, n_rows // n, -1).permute(*perm_batch, -2, -3, -1)
        res = y.reshape(*batch_shape, n_rows, -1)
        if left_tensor is not None:
            res = left_tensor @ res
        return res


class KroneckerProductTriangularLazyTensor(KroneckerProductLazyTensor):
    def __init__(self, *lazy_tensors, upper=False):
        from .triangular_lazy_tensor import TriangularLazyTensor

        if not all(isinstance(lt, TriangularLazyTensor) for lt in lazy_tensors):
            raise RuntimeError("Components of KroneckerProductTriangularLazyTensor must be TriangularLazyTensor.")
        super().__init__(*lazy_tensors)
        self.upper = upper

    def _cholesky_solve(self, rhs, upper=False):
        if upper:
            # res = (U.T @ U)^-1 @ v = U^-1 @ U^-T @ v
            w = self._transpose_nonbatch().inv_matmul(rhs)
            res = self.inv_matmul(w)
        else:
            # res = (L @ L.T)^-1 @ v = L^-T @ L^-1 @ v
            w = self.inv_matmul(rhs)
            res = self._transpose_nonbatch().inv_matmul(w)
        return res

    @cached
    def inverse(self):
        # here we use that (A \kron B)^-1 = A^-1 \kron B^-1
        inverses = [lt.inverse() for lt in self.lazy_tensors]
        return self.__class__(*inverses, upper=self.upper)

    def inv_matmul(self, right_tensor, left_tensor=None):
        # For triangular components, using triangular-triangular substition should generally be good
        return self._inv_matmul(right_tensor=right_tensor, left_tensor=left_tensor)
