import copy
import importlib
from abc import abstractmethod
from contextlib import nullcontext
from numbers import Number
from typing import TYPE_CHECKING, Tuple, Union, TypeVar, List, Sequence

import dask
import dask.array as da
import numpy as np
import zarr
from dask.diagnostics import ProgressBar

from abtem.core import config
from abtem.core.axes import HasAxes, UnknownAxis, axis_to_dict, axis_from_dict, AxisMetadata, OrdinalAxis
from abtem.core.backend import get_array_module, copy_to_device, device_name_from_array_module
from abtem.core.chunks import Chunks
from abtem.core.utils import normalize_axes, CopyMixin

if TYPE_CHECKING:
    pass


class ComputableList(list):

    def _get_computables(self):
        computables = []

        for computable in self:
            if hasattr(computable, 'array'):
                computables.append(computable.array)
            else:
                computables.append(computable)

        return computables

    def compute(self, **kwargs):
        if config.get('progress_bar'):
            progress_bar = ProgressBar()
        else:
            progress_bar = nullcontext()

        with progress_bar:
            arrays = dask.compute(self._get_computables(), **kwargs)[0]

        for array, wrapper in zip(arrays, self):
            wrapper._array = array

        return self

    def visualize_graph(self, **kwargs):
        return dask.visualize(self._get_computables(), **kwargs)


def _compute(dask_array_wrappers, progress_bar=None, **kwargs):
    if progress_bar is None:
        progress_bar = config.get('progress_bar')

    if progress_bar:
        progress_bar = ProgressBar()
    else:
        progress_bar = nullcontext()

    with progress_bar:
        arrays = dask.compute([wrapper.array for wrapper in dask_array_wrappers], **kwargs)[0]

    for array, wrapper in zip(arrays, dask_array_wrappers):
        wrapper._array = array

    return dask_array_wrappers


def compute(dask_array_wrappers, **kwargs):
    return _compute(dask_array_wrappers, **kwargs)


def computable(func):
    def wrapper(*args, compute=False, **kwargs):
        result = func(*args, **kwargs)

        if isinstance(result, tuple) and compute:
            return _compute(result)

        if compute:
            return result.compute()

        return result

    return wrapper


def validate_lazy(lazy):
    if lazy is None:
        return config.get('dask.lazy')

    return lazy


T = TypeVar('T', bound='HasArray')


class HasArray(HasAxes, CopyMixin):
    _array: Union[np.ndarray, da.core.Array]
    _base_dims: int

    @abstractmethod
    def __init__(self, *args, **kwargs):
        pass

    def from_array_and_metadata(self, array, axes_metadata, metadata):
        raise NotImplementedError

    @property
    def metadata(self):
        raise NotImplementedError

    @property
    def base_shape(self) -> Tuple[int, ...]:
        return self.array.shape[-self._base_dims:]

    @property
    def ensemble_shape(self) -> Tuple[int, ...]:
        return self.array.shape[:-self._base_dims]

    def __len__(self) -> int:
        return len(self.array)

    @property
    def chunks(self):
        return self.array.chunks

    def rechunk(self, *args, **kwargs):
        return self.array.rechunk(*args, **kwargs)

    @property
    def array(self) -> Union[np.ndarray, da.core.Array]:
        return self._array

    @property
    def dtype(self) -> np.dtype.base:
        return self._array.dtype

    @property
    def device(self):
        return device_name_from_array_module(get_array_module(self.array))

    @property
    def is_lazy(self):
        return isinstance(self.array, da.core.Array)

    @classmethod
    def _to_delayed_func(cls, array, **kwargs):
        kwargs['array'] = array

        return cls(**kwargs)

    def get_items(self, items, keep_dims: bool = False) -> 'T':
        if isinstance(items, (Number, slice)):
            items = (items,)
        elif not isinstance(items, tuple):
            raise NotImplementedError('indices must be integers or slices, or a tuple of integers or slices')

        if keep_dims:
            items = tuple(slice(item, item + 1) if isinstance(item, int) else item for item in items)

        if isinstance(items, tuple) and len(items) > len(self.ensemble_shape):
            raise RuntimeError('base axes cannot be indexed')

        axis_to_remove = []
        for i, item in enumerate(items):
            if isinstance(item, Number):
                axis_to_remove.append(i)
            elif isinstance(item, (type(...), type(None))):
                raise NotImplementedError

        if self._is_base_axis(axis_to_remove):
            raise RuntimeError('base axes cannot be indexed')

        new_axes_metadata = []
        last_indexed = 0
        for i, (axes_metadata, item) in enumerate(zip(self.ensemble_axes_metadata, items)):
            last_indexed += 1

            if i in axis_to_remove:
                continue

            if isinstance(axes_metadata, OrdinalAxis):
                new_axes_metadata += [axes_metadata[item]]
            else:
                new_axes_metadata += [axes_metadata]

        new_axes_metadata += self.ensemble_axes_metadata[last_indexed:]

        d = self.copy_kwargs(exclude=('array', 'ensemble_axes_metadata'))
        d['array'] = self._array[items]
        d['ensemble_axes_metadata'] = new_axes_metadata
        return self.__class__(**d)

    def __getitem__(self, items) -> 'T':
        return self.get_items(items)

    def to_delayed(self):
        return dask.delayed(self._to_delayed_func)(self.array, self.copy_kwargs(exclude=('array',)))

    def expand_dims(self, axis: Tuple[int, ...] = None, axis_metadata: List[AxisMetadata] = None) -> 'T':
        if axis is None:
            axis = (0,)

        if type(axis) not in (tuple, list):
            axis = (axis,)

        if axis_metadata is None:
            axis_metadata = [UnknownAxis()] * len(axis)

        axis = normalize_axes(axis, self.shape)

        if any(a >= (len(self.ensemble_shape) + len(axis)) for a in axis):
            raise RuntimeError()

        ensemble_axes_metadata = copy.deepcopy(self.ensemble_axes_metadata)

        for a, am in zip(axis, axis_metadata):
            ensemble_axes_metadata.insert(a, am)

        kwargs = self.copy_kwargs(exclude=('array', 'ensemble_axes_metadata'))
        kwargs['array'] = np.expand_dims(self.array, axis=axis)
        kwargs['ensemble_axes_metadata'] = ensemble_axes_metadata
        return self.__class__(**kwargs)

    def squeeze(self, axis: Tuple[int, ...] = None) -> 'T':
        if len(self.array.shape) < len(self.base_shape):
            return self

        if axis is None:
            axis = range(len(self.shape))
        else:
            axis = normalize_axes(axis, self.shape)

        shape = self.shape[:-len(self.base_shape)]

        squeezed = tuple(np.where([(n == 1) and (i in axis) for i, n in enumerate(shape)])[0])

        xp = get_array_module(self.array)

        kwargs = self.copy_kwargs(exclude=('array', 'ensemble_axes_metadata'))

        kwargs['array'] = xp.squeeze(self.array, axis=squeezed)
        kwargs['ensemble_axes_metadata'] = [element for i, element in enumerate(self.ensemble_axes_metadata) if
                                            i not in squeezed]

        return self.__class__(**kwargs)

    def ensure_lazy(self, chunks='auto') -> 'T':

        if self.is_lazy:
            return self

        chunks = ('auto',) * len(self.ensemble_shape) + (-1,) * len(self.base_shape)

        array = da.from_array(self.array, chunks=chunks)

        return self.__class__(array, **self.copy_kwargs(exclude=('array',)))

    def compute(self, progress_bar: bool = None, **kwargs):
        if not self.is_lazy:
            return self

        return _compute([self], **kwargs)[0]

    def visualize_graph(self, **kwargs):
        return self.array.visualize(**kwargs)

    def copy_to_device(self, device: str) -> 'T':
        """Copy array to specified device."""
        kwargs = self.copy_kwargs(exclude=('array',))
        kwargs['array'] = copy_to_device(self.array, device)
        return self.__class__(**kwargs)

    def to_cpu(self) -> 'T':
        return self.copy_to_device('cpu')

    def to_gpu(self) -> 'T':
        return self.copy_to_device('gpu')

    def to_zarr(self, url: str, compute: bool = True, overwrite: bool = False):
        """
        Write wave functions to a zarr file.

        Parameters
        ----------
        url : str
            Location of the data, typically a path to a local file. A URL can also include a protocol specifier like
            s3:// for remote data.
        overwrite : bool
            If given array already exists, overwrite=False will cause an error, where overwrite=True will replace the
            existing data.
        """

        with zarr.open(url, mode='w') as root:
            waves = self.ensure_lazy()

            array = waves.copy_to_device('cpu').array

            stored = array.to_zarr(url, compute=compute, component='array', overwrite=overwrite)
            for key, value in waves.copy_kwargs(exclude=('array',)).items():
                if key == 'ensemble_axes_metadata':
                    root.attrs[key] = [axis_to_dict(axis) for axis in value]
                else:
                    root.attrs[key] = value

            root.attrs['type'] = self.__class__.__name__

        return stored

    @classmethod
    def from_zarr(cls, url, chunks: int = 'auto') -> 'T':
        """
        Read wave functions from a hdf5 file.

        url : str
            Location of the data, typically a path to a local file. A URL can also include a protocol specifier like
            s3:// for remote data.
        chunks : int, optional
        """

        with zarr.open(url, mode='r') as f:
            kwargs = {}

            for key, value in f.attrs.items():
                if key == 'ensemble_axes_metadata':
                    ensemble_axes_metadata = [axis_from_dict(d) for d in value]
                elif key == 'type':
                    pass
                #    cls = globals()[value]
                else:

                    kwargs[key] = value

        if chunks == 'auto':
            chunks = ('auto',) * len(ensemble_axes_metadata) + (-1,) * cls._base_dims

        array = da.from_zarr(url, component='array', chunks=chunks)
        return cls(array, ensemble_axes_metadata=ensemble_axes_metadata, **kwargs)


def from_zarr(url: str, chunks: Chunks = None):
    import abtem

    with zarr.open(url, mode='r') as f:
        name = f.attrs['type']

    cls = getattr(abtem, name)
    return cls.from_zarr(url, chunks)


def stack(has_arrays: Sequence[HasArray], axes_metadata: AxisMetadata, axis: int = 0) -> 'T':
    xp = get_array_module(has_arrays[0].array)

    assert axis <= len(has_arrays[0].ensemble_shape)

    if has_arrays[0].is_lazy:
        array = da.stack([measurement.array for measurement in has_arrays], axis=axis)
    else:
        array = xp.stack([measurement.array for measurement in has_arrays], axis=axis)

    cls = has_arrays[0].__class__
    kwargs = has_arrays[0].copy_kwargs(exclude=('array',))

    kwargs['array'] = array
    kwargs['ensemble_axes_metadata'] = [axes_metadata] + kwargs['ensemble_axes_metadata']
    return cls(**kwargs)


def concatenate(has_arrays: Sequence[HasArray], axis: bool = 0) -> 'T':
    xp = get_array_module(has_arrays[0].array)

    if has_arrays[0].is_lazy:
        array = da.concatenate([has_array.array for has_array in has_arrays], axis=axis)
    else:
        array = xp.concatenate([has_array.array for has_array in has_arrays], axis=axis)

    cls = has_arrays[0].__class__

    concatenated_axes_metadata = has_arrays[0].axes_metadata[axis]
    for has_array in has_arrays[1:]:
        concatenated_axes_metadata = concatenated_axes_metadata.concatenate(has_array.axes_metadata[axis])

    axes_metadata = copy.deepcopy(has_arrays[0].axes_metadata)
    axes_metadata[axis] = concatenated_axes_metadata

    return cls.from_array_and_metadata(array=array,
                                       axes_metadata=axes_metadata,
                                       metadata=has_arrays[0].metadata)