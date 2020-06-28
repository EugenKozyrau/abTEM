import numba as nb
import numpy as np
from numba import jit, prange


# @nb.vectorize([nb.complex64(nb.float32), nb.complex128(nb.float64)])
def complex_exponential(x):
    return np.cos(x) + 1.j * np.sin(x)


@nb.vectorize([nb.float32(nb.complex64), nb.float64(nb.complex128)])
def abs2(x):
    return x.real ** 2 + x.imag ** 2


@jit(nopython=True, nogil=True, parallel=True)
def interpolate_radial_functions(array, array_rows, array_cols, indices, disc_indices, positions, v, r, dvdr, sampling):
    array = array.ravel()
    for i in range(indices.shape[0]):
        for j in prange(disc_indices.shape[0]):
            k = indices[i] + disc_indices[j]
            if k < array.shape[0]:
                r_interp = np.sqrt((array_rows[k] * sampling[0] - positions[i, 0]) ** 2 +
                                   (array_cols[k] * sampling[1] - positions[i, 1]) ** 2)

                idx = max(np.searchsorted(r, r_interp) - 1, 0)
                #if idx < dvdr.shape[1] - 1:
                array[k] += v[i, idx] + (r_interp - r[idx]) * dvdr[i, idx]


@jit(nopython=True, nogil=True)
def window_and_collapse(probes: np.ndarray, S: np.ndarray, corners, coefficients):
    """
    Function for collapsing a Prism scattering matrix into a probe wave function.

    Parameters
    ----------
    probes : 3d numpy.ndarray
        The array in which the probe wave functions should be written.
    S : 3d numpy.ndarray
        Scattering matrix
    corners :
    coefficients :
    """
    N, M = S.shape[1:]
    n, m = probes.shape[1:]
    for k in range(len(corners)):
        i, j = corners[k]
        ti = n - (N - i)
        tj = m - (M - j)
        if (i + n <= N) & (j + m <= M):
            for l in range(len(coefficients[k])):
                probes[k, :] += S[l, i:i + n, j:j + m] * coefficients[k][l]

        elif (i + n <= N) & (j + m > M):
            for l in range(len(coefficients[k])):
                probes[k, :, :-tj] += S[l, i:i + n, j:] * coefficients[k][l]
                probes[k, :, -tj:] += S[l, i:i + n, :tj] * coefficients[k][l]

        elif (i + n > N) & (j + m <= M):
            for l in range(len(coefficients[k])):
                probes[k, :-ti, :] += S[l, i:, j:j + m] * coefficients[k][l]
                probes[k, -ti:, :] += S[l, :ti, j:j + m] * coefficients[k][l]

        elif (i + n > N) & (j + m > M):
            for l in range(len(coefficients[k])):
                probes[k, :-ti, :-tj] += S[l, i:, j:] * coefficients[k][l]
                probes[k, :-ti, -tj:] += S[l, i:, :tj] * coefficients[k][l]
                probes[k, -ti:, -tj:] += S[l, :ti, :tj] * coefficients[k][l]
                probes[k, -ti:, :-tj] += S[l, :ti, j:] * coefficients[k][l]