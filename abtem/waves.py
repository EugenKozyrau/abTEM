"""Module for describing wave functions of the incoming electron beam and the exit wave."""
from __future__ import annotations

import itertools
from numbers import Number
import warnings
from abc import abstractmethod
from copy import copy
from functools import partial
from typing import Sequence

import dask
import dask.array as da
import numpy as np
from ase import Atoms

import abtem
from abtem.array import ArrayObject, _validate_lazy, ComputableList, expand_dims
from abtem.core.axes import (
    RealSpaceAxis,
    ReciprocalSpaceAxis,
    AxisMetadata,
    AxesMetadataList,
)
from abtem.core.backend import (
    get_array_module,
    validate_device,
    device_name_from_array_module,
)
from abtem.core.chunks import validate_chunks
from abtem.core.complex import abs2
from abtem.core.energy import Accelerator
from abtem.core.energy import HasAcceleratorMixin
from abtem.core.ensemble import (
    EmptyEnsemble,
    Ensemble,
    _wrap_with_array,
    unpack_blockwise_args,
)
from abtem.core.fft import fft2, ifft2, fft_crop, fft_interpolate
from abtem.core.grid import Grid, validate_gpts, polar_spatial_frequencies
from abtem.core.grid import HasGridMixin
from abtem.core.utils import (
    safe_floor_int,
    CopyMixin,
    EqualityMixin,
    tuple_range,
    interleave,
)
from abtem.detectors import (
    BaseDetector,
    _validate_detectors,
    WavesDetector,
    FlexibleAnnularDetector,
)
from abtem.distributions import BaseDistribution, EnsembleFromDistributions
from abtem.measurements import (
    DiffractionPatterns,
    Images,
    BaseMeasurements,
    RealSpaceLineProfiles,
)
from abtem.multislice import MultisliceTransform
from abtem.potentials.iam import BasePotential, _validate_potential
from abtem.scan import BaseScan, GridScan, _validate_scan
from abtem.tilt import validate_tilt
from abtem.transfer import Aberrations, CTF, Aperture, BaseAperture
from abtem.transform import (
    CompositeArrayObjectTransform,
    ArrayObjectTransform,
    EmptyTransform,
)


def _extract_measurement(array, index):
    if array.size == 0:
        return array

    array = array.item()[index].array
    return array


def _wrap_measurements(measurements):
    return measurements[0] if len(measurements) == 1 else ComputableList(measurements)


def _finalize_lazy_measurements(
    arrays, waves, detectors, extra_ensemble_axes_metadata=None, chunks=None
):

    if extra_ensemble_axes_metadata is None:
        extra_ensemble_axes_metadata = []

    measurements = []
    for i, detector in enumerate(detectors):

        base_shape = detector._out_base_shape(waves)
        meta = detector._out_meta(waves)

        new_axis = tuple(range(len(arrays.shape), len(arrays.shape) + len(base_shape)))

        if chunks is None:
            chunks = arrays.chunks

        array = arrays.map_blocks(
            _extract_measurement,
            i,
            chunks=chunks + tuple((n,) for n in base_shape),
            new_axis=new_axis,
            meta=meta,
        )

        ensemble_axes_metadata = detector._out_ensemble_axes_metadata(waves)

        base_axes_metadata = detector._out_base_axes_metadata(waves)

        axes_metadata = ensemble_axes_metadata + base_axes_metadata

        metadata = detector._out_metadata(waves)

        cls = detector._out_type(waves)

        axes_metadata = extra_ensemble_axes_metadata + axes_metadata

        measurement = cls.from_array_and_metadata(
            array, axes_metadata=axes_metadata, metadata=metadata
        )

        # measurement = detector._pack_single_output(waves, array)

        if hasattr(measurement, "reduce_ensemble"):
            measurement = measurement.reduce_ensemble()

        measurements.append(measurement)

    return measurements


def _ensure_parity(n, even, v=1):
    assert (v == 1) or (v == -1)
    assert isinstance(even, bool)

    if n % 2 == 0 and not even:
        return n + v
    elif not n % 2 == 0 and even:
        return n + v
    return n


def _ensure_parity_of_gpts(new_gpts, old_gpts, parity):
    if parity == "same":
        return (
            _ensure_parity(new_gpts[0], old_gpts[0] % 2 == 0),
            _ensure_parity(new_gpts[1], old_gpts[1] % 2 == 0),
        )
    elif parity == "odd":
        return (
            _ensure_parity(new_gpts[0], even=False),
            _ensure_parity(new_gpts[1], even=False),
        )
    elif parity == "even":
        return (
            _ensure_parity(new_gpts[0], even=True),
            _ensure_parity(new_gpts[1], even=True),
        )
    elif parity != "none":
        raise ValueError()


def _antialias_cutoff_gpts(gpts, sampling):
    kcut = 2.0 / 3.0 / max(sampling)
    extent = gpts[0] * sampling[0], gpts[1] * sampling[1]
    new_gpts = safe_floor_int(kcut * extent[0]), safe_floor_int(kcut * extent[1])
    return _ensure_parity_of_gpts(new_gpts, gpts, parity="same")


class BaseWaves(HasGridMixin, HasAcceleratorMixin):
    """Base class of all wave functions. Documented in the subclasses."""

    @property
    @abstractmethod
    def device(self):
        pass

    @property
    def dtype(self):
        return np.complex64

    @property
    @abstractmethod
    def metadata(self) -> dict:
        """Metadata stored as a dictionary."""
        pass

    @property
    def base_axes_metadata(self) -> list[AxisMetadata]:
        """List of AxisMetadata for the base axes in real space."""
        self.grid.check_is_defined()
        return [
            RealSpaceAxis(
                label="x", sampling=self.sampling[0], units="Å", endpoint=False
            ),
            RealSpaceAxis(
                label="y", sampling=self.sampling[1], units="Å", endpoint=False
            ),
        ]

    @property
    def reciprocal_space_axes_metadata(self) -> list[AxisMetadata]:
        """List of AxisMetadata for base axes in reciprocal space."""
        self.grid.check_is_defined()
        self.accelerator.check_is_defined()
        return [
            ReciprocalSpaceAxis(
                label="scattering angle x",
                sampling=self.angular_sampling[0],
                units="mrad",
            ),
            ReciprocalSpaceAxis(
                label="scattering angle y",
                sampling=self.angular_sampling[1],
                units="mrad",
            ),
        ]

    @property
    def antialias_cutoff_gpts(self) -> tuple[int, int]:
        """
        The number of grid points along the x and y direction in the simulation grid at the antialiasing cutoff
        scattering angle.
        """
        if "adjusted_antialias_cutoff_gpts" in self.metadata:
            n = min(self.metadata["adjusted_antialias_cutoff_gpts"][0], self.gpts[0])
            m = min(self.metadata["adjusted_antialias_cutoff_gpts"][1], self.gpts[1])
            return n, m

        self.grid.check_is_defined()
        return _antialias_cutoff_gpts(self.gpts, self.sampling)

    @property
    def antialias_valid_gpts(self) -> tuple[int, int]:
        """
        The number of grid points along the x and y direction in the simulation grid for the largest rectangle that fits
        within antialiasing cutoff scattering angle.
        """
        cutoff_gpts = self.antialias_cutoff_gpts
        valid_gpts = (
            safe_floor_int(cutoff_gpts[0] / np.sqrt(2)),
            safe_floor_int(cutoff_gpts[1] / np.sqrt(2)),
        )

        valid_gpts = _ensure_parity_of_gpts(valid_gpts, self.gpts, parity="same")

        if "adjusted_antialias_cutoff_gpts" in self.metadata:
            n = min(self.metadata["adjusted_antialias_cutoff_gpts"][0], valid_gpts[0])
            m = min(self.metadata["adjusted_antialias_cutoff_gpts"][1], valid_gpts[1])
            return n, m

        return valid_gpts

    def _gpts_within_angle(
        self, angle: float | str, parity: str = "same"
    ) -> tuple[int, int]:

        if angle is None or angle == "full":
            return self.gpts

        elif isinstance(angle, (Number, float)):
            gpts = (
                int(2 * np.ceil(angle / self.angular_sampling[0])) + 1,
                int(2 * np.ceil(angle / self.angular_sampling[1])) + 1,
            )

        elif angle == "cutoff":
            gpts = self.antialias_cutoff_gpts

        elif angle == "valid":
            gpts = self.antialias_valid_gpts

        else:
            raise ValueError(
                "Angle must be a number or one of 'cutoff', 'valid' or 'full'"
            )

        return _ensure_parity_of_gpts(gpts, self.gpts, parity=parity)

    @property
    def cutoff_angles(self) -> tuple[float, float]:
        """Scattering angles at the antialias cutoff [mrad]."""
        return (
            self.antialias_cutoff_gpts[0] // 2 * self.angular_sampling[0],
            self.antialias_cutoff_gpts[1] // 2 * self.angular_sampling[1],
        )

    @property
    def rectangle_cutoff_angles(self) -> tuple[float, float]:
        """Scattering angles corresponding to the sides of the largest rectangle within the antialias cutoff [mrad]."""
        return (
            self.antialias_valid_gpts[0] // 2 * self.angular_sampling[0],
            self.antialias_valid_gpts[1] // 2 * self.angular_sampling[1],
        )

    @property
    def full_cutoff_angles(self) -> tuple[float, float]:
        """Scattering angles corresponding to the full wave function size [mrad]."""
        return (
            self.gpts[0] // 2 * self.angular_sampling[0],
            self.gpts[1] // 2 * self.angular_sampling[1],
        )

    @property
    def angular_sampling(self) -> tuple[float, float]:
        """Reciprocal-space sampling in units of scattering angles [mrad]."""
        self.accelerator.check_is_defined()
        fourier_space_sampling = self.reciprocal_space_sampling
        return (
            fourier_space_sampling[0] * self.wavelength * 1e3,
            fourier_space_sampling[1] * self.wavelength * 1e3,
        )

    def _angular_grid(self):
        xp = get_array_module(self.device)
        alpha, phi = polar_spatial_frequencies(self.gpts, self.sampling, xp=xp)
        alpha *= self.wavelength
        return alpha, phi


class _WaveRenormalization(EmptyEnsemble, ArrayObjectTransform):
    def _calculate_new_array(self, array_object) -> np.ndarray | tuple[np.ndarray, ...]:
        array = array_object.normalize().array
        return array


class Waves(BaseWaves, ArrayObject):
    """
    Waves define a batch of arbitrary 2D wave functions defined by a complex array.

    Parameters
    ----------
    array : array
        Complex array defining one or more 2D wave functions. The second-to-last and last dimensions are the wave
        function `y`- and `x`-axes, respectively.
    energy : float
        Electron energy [eV].
    extent : one or two float
        Extent of wave functions in `x` and `y` [Å].
    sampling : one or two float
        Sampling of wave functions in `x` and `y` [1 / Å].
    reciprocal_space : bool, optional
        If True, the wave functions are assumed to be represented in reciprocal space instead of real space (default is
        False).
    ensemble_axes_metadata : list of AxesMetadata
        Axis metadata for each ensemble axis. The axis metadata must be compatible with the shape of the array.
    metadata : dict
        A dictionary defining wave function metadata. All items will be added to the metadata of measurements derived
        from the waves.
    """

    def __init__(
        self,
        array: np.ndarray,
        energy: float,
        extent: float | tuple[float, float] = None,
        sampling: float | tuple[float, float] = None,
        reciprocal_space: bool = False,
        ensemble_axes_metadata: list[AxisMetadata] = None,
        metadata: dict = None,
    ):
        self._grid = Grid(
            extent=extent, gpts=array.shape[-2:], sampling=sampling, lock_gpts=True
        )
        self._accelerator = Accelerator(energy=energy)
        self._reciprocal_space = reciprocal_space

        super().__init__(
            array=array,
            base_dims=2,
            ensemble_axes_metadata=ensemble_axes_metadata,
            metadata=metadata,
        )

    @property
    def device(self) -> str:
        """The device where the array is stored."""
        return device_name_from_array_module(get_array_module(self.array))

    @property
    def base_tilt(self):
        """
        The base small-angle beam tilt (i.e. the beam tilt not associated with an ensemble axis) applied to the Fresnel
        propagator [mrad].
        """
        return (
            self.metadata.get("base_tilt_x", 0.0),
            self.metadata.get("base_tilt_y", 0.0),
        )

    @property
    def reciprocal_space(self):
        """True if the waves are represented in reciprocal space."""
        return self._reciprocal_space

    @property
    def metadata(self) -> dict:
        self._metadata["energy"] = self.energy
        self._metadata["reciprocal_space"] = self.reciprocal_space
        return self._metadata

    @classmethod
    def from_array_and_metadata(
        cls, array: np.ndarray, axes_metadata: list[AxisMetadata], metadata: dict = None
    ) -> Waves:

        """
        Creates wave functions from a given array and metadata.

        Parameters
        ----------
        array : array
            Complex array defining one or more 2D wave functions. The second-to-last and last dimensions are the wave
            function `y`- and `x`-axis, respectively.
        axes_metadata : list of AxesMetadata
            Axis metadata for each axis. The axis metadata must be compatible with the shape of the array. The last two
            axes must be RealSpaceAxis.
        metadata :
            A dictionary defining wave function metadata. All items will be added to the metadata of measurements
            derived from the waves. The metadata must contain the electron energy [eV].

        Returns
        -------
        wave_functions : Waves
            The created wave functions.
        """
        energy = metadata["energy"]
        reciprocal_space = metadata.get("reciprocal_space", False)

        x_axis, y_axis = axes_metadata[-2], axes_metadata[-1]

        if isinstance(x_axis, RealSpaceAxis) and isinstance(y_axis, RealSpaceAxis):
            sampling = x_axis.sampling, y_axis.sampling
        else:
            raise ValueError()

        return cls(
            array,
            sampling=sampling,
            energy=energy,
            reciprocal_space=reciprocal_space,
            ensemble_axes_metadata=axes_metadata[:-2],
            metadata=metadata,
        )

    def convolve(
        self,
        kernel: np.ndarray,
        axes_metadata: list[AxisMetadata] = None,
        out_space: str = "in_space",
        in_place: bool = False,
    ):
        """
        Convolve the wave-function array with a given array.

        Parameters
        ----------
        kernel : np.ndarray
            Array to be convolved with.
        axes_metadata : list of AxisMetadata, optional
            Metadata for the resulting convolved array. Needed only if the given array has more than two dimensions.
        out_space : str, optional
            Space in which the convolved array is represented. Options are 'reciprocal_space' and 'real_space' (default
            is the space of the wave functions).
        in_place : bool, optional
            If True, the array representing the waves may be modified in-place.

        Returns
        -------
        convolved : Waves
            The convolved wave functions.
        """

        if out_space == "in_space":
            fourier_space_out = self.reciprocal_space
        elif out_space in ("reciprocal_space", "real_space"):
            fourier_space_out = out_space == "reciprocal_space"
        else:
            raise ValueError

        if axes_metadata is None:
            axes_metadata = []

        if (len(kernel.shape) - 2) != len(axes_metadata):
            raise ValueError("provide axes metadata for each ensemble axis")

        waves = self.ensure_reciprocal_space(overwrite_x=in_place)
        waves_dims = tuple(range(len(kernel.shape) - 2))
        kernel_dims = tuple(
            range(
                len(kernel.shape) - 2,
                len(waves.array.shape) - 2 + len(kernel.shape) - 2,
            )
        )

        kernel = expand_dims(kernel, axis=kernel_dims)
        array = expand_dims(waves._array, axis=waves_dims)

        xp = get_array_module(self.device)

        kernel = xp.array(kernel)

        if in_place and (array.shape == kernel.shape):
            array *= kernel
        else:
            array = array * kernel

        if not fourier_space_out:
            array = ifft2(array, overwrite_x=in_place)

        d = waves._copy_kwargs(exclude=("array",))
        d["reciprocal_space"] = fourier_space_out
        d["array"] = array
        d["ensemble_axes_metadata"] = axes_metadata + d["ensemble_axes_metadata"]
        return waves.__class__(**d)

    def normalize(self, space: str = "reciprocal", overwrite_x: bool = False):
        """
        Normalize the wave functions in real or reciprocal space.

        Parameters
        ----------
        space : str
            Should be one of 'real' or 'reciprocal' (default is 'reciprocal'). Defines whether the wave function should
            be normalized such that the intensity sums to one in real or reciprocal space.

        Returns
        -------
        normalized_waves : Waves
            The normalized wave functions.
        """

        if self.is_lazy:
            return self.apply_transform(_WaveRenormalization())

        xp = get_array_module(self.device)

        reciprocal_space = self.reciprocal_space

        if space == "reciprocal":
            waves = self.ensure_reciprocal_space(overwrite_x=overwrite_x)
            f = xp.sqrt(abs2(waves.array).sum((-2, -1), keepdims=True))
            if overwrite_x:
                waves._array /= f
            else:
                waves._array = waves._array / f

            if not reciprocal_space:
                waves = waves.ensure_real_space(overwrite_x=overwrite_x)

        elif space == "real":
            raise NotImplementedError
        else:
            raise ValueError()

        return waves

    def tile(self, repetitions: tuple[int, int], renormalize: bool = False) -> Waves:
        """
        Tile the wave functions. Can only be applied in real space.

        Parameters
        ----------
        repetitions : two int
            The number of repetitions of the wave functions along the `x`- and `y`-axes.
        renormalize : bool, optional
            If True, preserve the total intensity of the wave function (default is False).

        Returns
        -------
        tiled_wave_functions : Waves
            The tiled wave functions.
        """

        xp = get_array_module(self.device)

        if self.reciprocal_space:
            raise NotImplementedError

        if self.is_lazy:
            tile_func = da.tile
        else:
            tile_func = xp.tile

        array = tile_func(self.array, (1,) * len(self.ensemble_shape) + repetitions)

        if hasattr(array, "rechunk"):
            array = array.rechunk(array.chunks[:-2] + (-1, -1))

        kwargs = self._copy_kwargs(exclude=("array", "extent"))
        kwargs["array"] = array

        if renormalize:
            kwargs["array"] /= xp.asarray(np.prod(repetitions))

        return self.__class__(**kwargs)

    def ensure_reciprocal_space(self, overwrite_x: bool = False):
        """
        Transform to reciprocal space if the wave functions are represented in real space.

        Parameters
        ----------
        overwrite_x : bool, optional
            If True, modify the array in place; otherwise a copy is created (default is False).

        Returns
        -------
        waves_in_reciprocal_space : Waves
            The wave functions in reciprocal space.
        """

        if self.reciprocal_space:
            return self

        d = self._copy_kwargs(exclude=("array",))
        d["array"] = fft2(self.array, overwrite_x=overwrite_x)
        d["reciprocal_space"] = True
        return self.__class__(**d)

    def ensure_real_space(self, overwrite_x: bool = False):
        """
        Transform to real space if the wave functions are represented in reciprocal space.

        Parameters
        ----------
        overwrite_x : bool, optional
            If True, modify the array in place; otherwise a copy is created (default is False).

        Returns
        -------
        waves_in_real_space : Waves
            The wave functions in real space.
        """

        if not self.reciprocal_space:
            return self

        d = self._copy_kwargs(exclude=("array",))
        d["array"] = ifft2(self.array, overwrite_x=overwrite_x)
        d["reciprocal_space"] = False
        waves = self.__class__(**d)
        return waves

    def phase_shift(self, amount: float):
        """
        Shift the phase of the wave functions.

        Parameters
        ----------
        amount : float
            Amount of phase shift [rad].

        Returns
        -------
        phase_shifted_waves : Waves
            The shifted wave functions.
        """

        def _phase_shift(array):
            xp = get_array_module(self.array)
            return xp.exp(1.0j * amount) * array

        d = self._copy_kwargs(exclude=("array",))
        d["array"] = _phase_shift(self.array)
        d["reciprocal_space"] = False
        return self.__class__(**d)

    def intensity(self) -> Images:
        """
        Calculate the intensity of the wave functions.

        Returns
        -------
        intensity_images : Images
            The intensity of the wave functions.
        """

        def _intensity(array):
            return abs2(array)

        metadata = copy(self.metadata)
        metadata["label"] = "intensity"
        metadata["units"] = "arb. unit"

        xp = get_array_module(self.array)

        if self.is_lazy:
            array = self.array.map_blocks(_intensity, dtype=xp.float32)
        else:
            array = _intensity(self.array)

        return Images(
            array,
            sampling=self.sampling,
            ensemble_axes_metadata=self.ensemble_axes_metadata,
            metadata=metadata,
        )

    def complex_images(self):
        """
        The complex array of the wave functions at the image plane.

        Returns
        -------
        complex_images : Images
            The wave functions as a complex image.
        """

        array = self.array.copy()
        metadata = copy(self.metadata)
        metadata["label"] = "intensity"
        metadata["units"] = "arb. unit"
        return Images(
            array,
            sampling=self.sampling,
            ensemble_axes_metadata=self.ensemble_axes_metadata,
            metadata=metadata,
        )

    def downsample(
        self,
        max_angle: str | float = "cutoff",
        gpts: tuple[int, int] = None,
        normalization: str = "values",
    ) -> Waves:
        """
        Downsample the wave functions to a lower maximum scattering angle.

        Parameters
        ----------
        max_angle : {'cutoff', 'valid'} or float, optional
            Controls the downsampling of the wave functions.

                ``cutoff`` :
                    Downsample to the antialias cutoff scattering angle (default).

                ``valid`` :
                    Downsample to the largest rectangle that fits inside the circle with a radius defined by the
                    antialias cutoff scattering angle.

                float :
                    Downsample to a maximum scattering angle specified by a float [mrad].

        gpts : two int, optional
            Number of grid points of the wave functions after downsampling. If given, `max_angle` is not used.

        normalization : {'values', 'amplitude'}
            The normalization parameter determines the preserved quantity after normalization.

                ``values`` :
                    The pixel-wise values of the wave function are preserved (default).

                ``amplitude`` :
                    The total amplitude of the wave function is preserved.

        Returns
        -------
        downsampled_waves : Waves
            The downsampled wave functions.
        """

        xp = get_array_module(self.array)

        if gpts is None:
            gpts = self._gpts_within_angle(max_angle)

        if self.is_lazy:
            array = self.array.map_blocks(
                fft_interpolate,
                new_shape=gpts,
                normalization=normalization,
                chunks=self.array.chunks[:-2] + gpts,
                meta=xp.array((), dtype=xp.complex64),
            )
        else:
            array = fft_interpolate(
                self.array, new_shape=gpts, normalization=normalization
            )

        kwargs = self._copy_kwargs(exclude=("array",))
        kwargs["array"] = array
        kwargs["sampling"] = (self.extent[0] / gpts[0], self.extent[1] / gpts[1])
        kwargs["metadata"][
            "adjusted_antialias_cutoff_gpts"
        ] = self.antialias_cutoff_gpts
        return self.__class__(**kwargs)

    def diffraction_patterns(
        self,
        max_angle: str | float = "cutoff",
        # max_frequency: str | float = None,
        block_direct: bool | float = False,
        fftshift: bool = True,
        parity: str = "odd",
        return_complex: bool = False,
        renormalize: bool = True,
    ) -> DiffractionPatterns:
        """
        Calculate the intensity of the wave functions at the diffraction plane.

        Parameters
        ----------
        max_angle : {'cutoff', 'valid', 'full'} or float
            Control the maximum scattering angle of the diffraction patterns.

                ``cutoff`` :
                    Downsample to the antialias cutoff scattering angle (default).

                ``valid`` :
                    Downsample to the largest rectangle that fits inside the circle with a radius defined by the
                    antialias cutoff scattering angle.

                ``full`` :
                    The diffraction patterns are not cropped, and hence the antialiased region is included.

                float :
                    Downsample to a maximum scattering angle specified by a float [mrad].

        block_direct : bool or float, optional
            If True the direct beam is masked (default is False). If given as a float, masks up to that scattering
            angle [mrad].
        fftshift : bool, optional
            If False, do not shift the direct beam to the center of the diffraction patterns (default is True).
        parity : {'same', 'even', 'odd', 'none'}
            The parity of the shape of the diffraction patterns. Default is 'odd', so that the shape of the diffraction
            pattern is odd with the zero at the middle.
        renormalize : bool, optional
            If true and the wave function intensities were normalized to sum to the number of pixels in real space, i.e.
            the default normalization of a plane wave, the intensities are to sum to one in reciprocal space.
        return_complex : bool
            If True, return complex-valued diffraction patterns (i.e. the wave function in reciprocal space)
            (default is False).

        Returns
        -------
        diffraction_patterns : DiffractionPatterns
            The diffraction pattern(s).
        """

        def _diffraction_pattern(array, new_gpts, return_complex, fftshift, normalize):
            xp = get_array_module(array)

            if normalize:
                array = array / np.prod(array.shape[-2:])

            array = fft2(array, overwrite_x=False)

            if array.shape[-2:] != new_gpts:
                array = fft_crop(array, new_shape=array.shape[:-2] + new_gpts)

            if not return_complex:
                array = abs2(array)

            if fftshift:
                return xp.fft.fftshift(array, axes=(-1, -2))

            return array

        xp = get_array_module(self.array)

        if max_angle is None:
            max_angle = "full"

        new_gpts = self._gpts_within_angle(max_angle, parity=parity)

        metadata = copy(self.metadata)
        metadata["label"] = "intensity"
        metadata["units"] = "arb. unit"

        normalize = False
        if renormalize and "normalization" in metadata:
            if metadata["normalization"] == "values":
                normalize = True
            elif metadata["normalization"] != "reciprocal_space":
                raise RuntimeError(
                    f"normalization {metadata['normalization']} not recognized"
                )

        validate_gpts(new_gpts)

        if self.is_lazy:
            dtype = xp.complex64 if return_complex else xp.float32

            pattern = self.array.map_blocks(
                _diffraction_pattern,
                new_gpts=new_gpts,
                fftshift=fftshift,
                return_complex=return_complex,
                normalize=normalize,
                chunks=self.array.chunks[:-2] + ((new_gpts[0],), (new_gpts[1],)),
                meta=xp.array((), dtype=dtype),
            )
        else:
            pattern = _diffraction_pattern(
                self.array,
                new_gpts=new_gpts,
                return_complex=return_complex,
                fftshift=fftshift,
                normalize=normalize,
            )

        diffraction_patterns = DiffractionPatterns(
            pattern,
            sampling=(
                self.reciprocal_space_sampling[0],
                self.reciprocal_space_sampling[1],
            ),
            fftshift=fftshift,
            ensemble_axes_metadata=self.ensemble_axes_metadata,
            metadata=metadata,
        )

        if block_direct:
            diffraction_patterns = diffraction_patterns.block_direct(
                radius=block_direct
            )

        return diffraction_patterns

    def apply_ctf(
        self, ctf: CTF = None, max_batch: int | str = "auto", **kwargs
    ) -> Waves:
        """
        Apply the aberrations and apertures of a contrast transfer function to the wave functions.

        Parameters
        ----------
        ctf : CTF, optional
            Contrast transfer function to be applied.
        max_batch : int, optional
            The number of wave functions in each chunk of the Dask array. If 'auto' (default), the batch size is
            automatically chosen based on the abtem user configuration settings "dask.chunk-size" and
            "dask.chunk-size-gpu".
        kwargs :
            Provide the parameters of the contrast transfer function as keyword arguments (see :class:`.CTF`).

        Returns
        -------
        aberrated_waves : Waves
            The wave functions with the contrast transfer function applied.
        """

        if ctf is None:
            ctf = CTF(**kwargs)

        if not ctf.accelerator.energy:
            ctf.accelerator.match(self.accelerator)

        self.accelerator.match(ctf.accelerator, check_match=True)
        self.accelerator.check_is_defined()

        return self.apply_transform(ctf, max_batch=max_batch)

    def multislice(
        self,
        potential: BasePotential,
        detectors: BaseDetector | list[BaseDetector] = None,
    ) -> Waves:
        """
        Propagate and transmit wave function through the provided potential using the multislice algorithm. When
        detector(s) are given, output will be the corresponding measurement.

        Parameters
        ----------
        potential : BasePotential or ASE.Atoms
            The potential through which to propagate the wave function. Optionally atoms can be directly given.
        detectors : BaseDetector or list of BaseDetector, optional
            A detector or a list of detectors defining how the wave functions should be converted to measurements after
            running the multislice algorithm. See `abtem.measurements.detect` for a list of implemented detectors. If
            not given, returns the wave functions themselves.
        conjugate : bool, optional
            If True, use the conjugate of the transmission function (default is False).
        transpose : bool, optional
            If True, reverse the order of propagation and transmission (default is False).

        Returns
        -------
        detected_waves : BaseMeasurements or list of BaseMeasurement
            The detected measurement (if detector(s) given).
        exit_waves : Waves
            Wave functions at the exit plane(s) of the potential (if no detector(s) given).
        """

        multislice_transform = MultisliceTransform(
            potential=potential, detectors=detectors
        )

        return self.apply_transform(transform=multislice_transform)

    def show(self, **kwargs):
        """
        Show the wave-function intensities.

        kwargs :
            Keyword arguments for `abtem.measurements.Images.show`.
        """
        return self.intensity().show(**kwargs)


def _reduce_ensemble(ensemble):
    if isinstance(ensemble, (list, tuple)):
        return [_reduce_ensemble(ensemble) for ensemble in ensemble]

    squeeze = ()
    for i, axes_metadata in enumerate(ensemble.ensemble_axes_metadata):
        if axes_metadata._squeeze:
            squeeze += (i,)

    output = ensemble.squeeze(squeeze)

    if hasattr(output, "reduce_ensemble"):
        output = output.reduce_ensemble()

    return output


class _WavesBuilder(EnsembleFromDistributions, BaseWaves):
    def __init__(
        self, distributions, transforms: list[ArrayObjectTransform], device: str
    ):

        if transforms is None:
            transforms = []

        self._transforms = transforms
        self._device = device

        super().__init__()

    @property
    def device(self):
        """The device where the waves are created."""
        return self._device

    @property
    def shape(self):
        """Shape of the waves."""
        return self.ensemble_shape + self.base_shape

    @property
    def base_shape(self) -> tuple[int, int]:
        """Shape of the base axes of the waves."""
        return self.gpts

    @property
    def ensemble_shape(self) -> tuple[int, ...]:
        """Shape of the ensemble axes of the waves."""
        return CompositeArrayObjectTransform(self.transforms).ensemble_shape

    @property
    def ensemble_axes_metadata(self) -> list[AxisMetadata]:
        """List of AxisMetadata of the ensemble axes."""
        return CompositeArrayObjectTransform(self.transforms).ensemble_axes_metadata

    @property
    def axes_metadata(self) -> AxesMetadataList:
        """List of AxisMetadata."""
        return AxesMetadataList(
            self.ensemble_axes_metadata + self.base_axes_metadata, self.shape
        )

    @property
    def tilt(self):
        """The small-angle tilt of applied to the Fresnel propagator [mrad]."""
        return self._tilt

    @tilt.setter
    def tilt(self, value):
        old_tilt = self.tilt
        new_tilt = validate_tilt(value)
        for i, transform in enumerate(self._transforms):
            if transform is old_tilt:
                self._transforms[i] = new_tilt

        self._tilt = new_tilt

    @abstractmethod
    def metadata(self):
        """Metadata describing the waves."""
        pass

    def insert_transform(
        self, transform: ArrayObjectTransform, index: int = None
    ) -> _WavesBuilder:
        """
        Insert a wave function transformation applied during the creation of the waves.

        Parameters
        ----------
        transform : ArrayObjectTransform
            Wave transform to apply during creation.
        index : int
            The position in the order of the applied transformations.
        """
        if index is None:
            index = len(self._transforms)

        self._transforms.insert(index, transform)
        return self

    @property
    def transforms(self):
        """The transforms applied during creation of the waves."""
        return self._transforms

    @staticmethod
    def _base_waves(
        gpts: float,
        extent: float,
        energy: float,
        reciprocal_space: bool,
        device: str,
        metadata: dict,
        lazy: bool,
        normalize: bool,
    ):
        xp = get_array_module(device)

        kwargs = {"dtype": xp.complex64, "shape": gpts}

        if normalize:
            func = xp.full
            kwargs["fill_value"] = 1 / np.prod(gpts)
        else:
            func = xp.ones

        if lazy:
            delayed_array = dask.delayed(func)(**kwargs)
            array = da.from_delayed(
                delayed_array, shape=gpts, meta=xp.array((), dtype=xp.complex64)
            )
        else:
            array = func(**kwargs)

        return Waves(
            array=array,
            energy=energy,
            extent=extent,
            reciprocal_space=reciprocal_space,
            metadata=metadata,
        )

    def _base_waves_partial(
        self,
        lazy: bool = False,
        reciprocal_space: bool = False,
        normalize: bool = False,
    ):
        return partial(
            self._base_waves,
            gpts=self.gpts,
            extent=self.extent,
            energy=self.energy,
            reciprocal_space=reciprocal_space,
            device=self.device,
            metadata=self.metadata,
            lazy=lazy,
            normalize=normalize,
        )

    @staticmethod
    def _lazy_build(
        *args,
        waves_partial,
        transform_partial,
    ):

        transform = transform_partial(*(arg.item() for arg in args))
        waves = waves_partial()
        array = transform._calculate_new_array(waves)

        if transform._num_outputs > 1:
            arr = np.zeros((1,) * len(args), dtype=object)
            arr.itemset(array)
            return arr

        return array

    def _lazy_build_transform(self, waves_partial, transform, max_batch):
        from abtem.core.chunks import validate_chunks

        if isinstance(max_batch, int):
            max_batch = int(max_batch * np.prod(self.base_shape))

        chunks = transform._default_ensemble_chunks + self.base_shape

        chunks = validate_chunks(
            transform.ensemble_shape + self.base_shape,
            chunks,
            limit=max_batch,
            dtype=self.dtype,
        )

        transform_chunks = chunks[: len(transform.ensemble_shape)]

        transform_args, transform_symbols = transform._get_blockwise_args(
            transform_chunks
        )

        dummy_waves = self._base_waves_partial(lazy=True, reciprocal_space=False)()

        transform.set_output_specification(dummy_waves)

        if transform._num_outputs > 1:
            num_ensemble_dims = len(transform.ensemble_shape)
            chunks = chunks[:num_ensemble_dims]
            symbols = tuple_range(num_ensemble_dims)
            new_axes = None
            meta = np.array((), dtype=object)
        else:
            base_shape = transform._out_base_shape(dummy_waves)
            num_ensemble_dims = len(transform._out_ensemble_shape(dummy_waves))
            symbols = tuple_range(num_ensemble_dims + len(base_shape))
            new_base_shape = base_shape[: len(symbols) - len(transform.ensemble_shape)]
            new_axes = {num_ensemble_dims + i: n for i, n in enumerate(new_base_shape)}
            chunks = chunks[: -len(base_shape)] + new_base_shape
            meta = transform._out_meta(dummy_waves)

        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message="Increasing number of chunks")
            new_array = da.blockwise(
                self._lazy_build,
                symbols,
                *interleave(transform_args, transform_symbols),
                adjust_chunks={i: chunk for i, chunk in enumerate(chunks)},
                transform_partial=transform._from_partitioned_args(),
                new_axes=new_axes,
                waves_partial=waves_partial,
                meta=meta,
                align_arrays=False,
                concatenate=True,
            )

        if transform._num_outputs > 1:
            outputs = transform._pack_multiple_outputs(dummy_waves, new_array)

            return ComputableList(_reduce_ensemble(outputs))
        else:
            output = transform._pack_single_output(dummy_waves, new_array)
            output = _reduce_ensemble(output)
            return output


class PlaneWave(Ensemble, BaseWaves):
    """
    Represents electron probe wave functions for simulating experiments with a plane-wave probe, such as HRTEM and SAED.

    Parameters
    ----------
    extent : two float, optional
        Lateral extent of the wave function [Å].
    gpts : two int, optional
        Number of grid points describing the wave function.
    sampling : two float, optional
        Lateral sampling of the wave functions [1 / Å]. If 'gpts' is also given, will be ignored.
    energy : float, optional
        Electron energy [eV]. If not provided, inferred from the wave functions.
    normalize : bool, optional
        If true, normalizes the wave function such that its reciprocal space intensity sums to one. If false, the
        wave function takes a value of one everywhere.
    tilt : two float, optional
        Small-angle beam tilt [mrad] (default is (0., 0.)). Implemented by shifting the wave functions at every slice.
    device : str, optional
        The wave functions are stored on this device ('cpu' or 'gpu'). The default is determined by the user
        configuration.
    transforms : list of WaveTransform, optional
        Can apply any transformation to the wave functions (e.g. to describe a phase plate).
    """

    def __init__(
        self,
        extent: float | tuple[float, float] = None,
        gpts: int | tuple[int, int] = None,
        sampling: float | tuple[float, float] = None,
        energy: float = None,
        normalize: bool = False,
        tilt: tuple[float, float] = (0.0, 0.0),
        device: str = None,
        transforms: list[ArrayObjectTransform] = None,
    ):

        self._grid = Grid(extent=extent, gpts=gpts, sampling=sampling)
        self._accelerator = Accelerator(energy=energy)
        self._tilt = validate_tilt(tilt=tilt)
        self._normalize = normalize
        device = validate_device(device)

        transforms = [] if transforms is None else transforms

        transforms = transforms + [self._tilt]

        # super().__init__(transforms=transforms, device=device)
        super().__init__(distributions=("tilt",))

    @property
    def tilt(self):
        return self._tilt

    @property
    def metadata(self):
        metadata = {
            "energy": self.energy,
            **self._tilt.metadata,
            "normalization": ("reciprocal_space" if self._normalize else "values"),
        }
        return metadata

    @property
    def normalize(self):
        """True if the created waves are normalized in reciprocal space."""
        return self._normalize

    # def _get_transforms(
    #     self,
    #     potential: BasePotential = None,
    #     detectors: BaseDetector | list[BaseDetector] = None,
    # ) -> CompositeArrayObjectTransform:
    #
    #     transforms = [*self.transforms]
    #
    #     if detectors is None:
    #         detectors = WavesDetector()
    #
    #     detectors = _validate_detectors(detectors)
    #
    #     if potential is not None:
    #         multislice = MultisliceTransform(potential, detectors)
    #
    #         transforms = [multislice, *transforms]
    #     else:
    #         assert len(detectors)
    #         transforms = [*detectors, *transforms]
    #
    #     transform = CompositeArrayObjectTransform(transforms)
    #
    #     return transform

    # def _build(self, potential=None, detectors=None, lazy=None, max_batch="auto"):
    #
    #     if potential is not None:
    #         potential = _validate_potential(potential)
    #         self.grid.match(potential)
    #
    #     self.grid.check_is_defined()
    #     self.accelerator.check_is_defined()
    #
    #     lazy = _validate_lazy(lazy)
    #
    #     transform = self._get_transforms(potential=potential, detectors=detectors)
    #
    #     waves_partial = self._base_waves_partial(
    #         lazy=False, reciprocal_space=False, normalize=self.normalize
    #     )
    #
    #     if lazy:
    #         measurements = self._lazy_build_transform(
    #             waves_partial, transform=transform, max_batch=max_batch
    #         )
    #     else:
    #         measurements = waves_partial()
    #         for transform in reversed(transform.transforms):
    #             measurements = transform.apply(measurements)
    #
    #         measurements = _reduce_ensemble(measurements)
    #
    #     return measurements

    def build(
        self,
        lazy: bool = None,
        max_batch: int | str = "auto",
    ) -> Waves:
        """
        Build plane-wave wave functions.

        Parameters
        ----------
        lazy : bool, optional
            If True, create the wave functions lazily, otherwise, calculate instantly. If not given, defaults to the
            setting in the user configuration file.
        max_batch : int or str, optional
            The number of wave functions in each chunk of the Dask array. If 'auto' (default), the batch size is
            automatically chosen based on the abtem user configuration settings "dask.chunk-size" and
            "dask.chunk-size-gpu".

        Returns
        -------
        plane_waves : Waves
            The wave functions.
        """
        return self._build(
            potential=None, detectors=None, lazy=lazy, max_batch=max_batch
        )

    def multislice(
        self,
        potential: BasePotential | Atoms,
        detectors: BaseDetector = None,
        max_batch: int | str = "auto",
        lazy: bool = None,
        ctf: CTF = None,
        transition_potentials=None,
    ) -> Waves:
        """
        Run the multislice algorithm, after building the plane-wave wave function as needed. The grid of the wave
        functions will be set to the grid of the potential.

        Parameters
        ----------
        potential : BasePotential, Atoms
            The potential through which to propagate the wave function. Optionally atoms can be directly given.
        detectors : Detector, list of detectors, optional
            A detector or a list of detectors defining how the wave functions should be converted to measurements after
            running the multislice algorithm.
        max_batch : int, optional
            The number of wave functions in each chunk of the Dask array. If 'auto' (default), the batch size is
            automatically chosen based on the abtem user configuration settings "dask.chunk-size" and
            "dask.chunk-size-gpu".
        lazy : bool, optional
            If True, create the wave functions lazily, otherwise, calculate instantly. If None, this defaults to the
            setting in the user configuration file.
        ctf : CTF, optional
            A contrast transfer function may be applied before detecting to save memory.
        transition_potentials : BaseTransitionPotential, optional
            Used to describe inelastic core losses.

        Returns
        -------
        detected_waves : BaseMeasurements or list of BaseMeasurement
            The detected measurement (if detector(s) given).
        exit_waves : Waves
            Wave functions at the exit plane(s) of the potential (if no detector(s) given).
        """

        return self._build(
            potential=potential, detectors=detectors, lazy=lazy, max_batch=max_batch
        )


class Probe(BaseWaves, Ensemble, CopyMixin, EqualityMixin):
    """
    Represents electron-probe wave functions for simulating experiments with a convergent beam,
    such as CBED and STEM.

    Parameters
    ----------
    semiangle_cutoff : float, optional
        The cutoff semiangle of the aperture [mrad]. Ignored if a custom aperture is given.
    extent : float or two float, optional
        Lateral extent of wave functions [Å] in `x` and `y` directions. If a single float is given, both are set equal.
    gpts : two ints, optional
        Number of grid points describing the wave functions.
    sampling : two float, optional
        Lateral sampling of wave functions [1 / Å]. If 'gpts' is also given, will be ignored.
    energy : float, optional
        Electron energy [eV]. If not provided, inferred from the wave functions.
    soft : float, optional
        Taper the edge of the default aperture [mrad] (default is 2.0). Ignored if a custom aperture is given.
    tilt : two float, two 1D :class:`.BaseDistribution`, 2D :class:`.BaseDistribution`, optional
        Small-angle beam tilt [mrad]. This value should generally not exceed one degree.
    device : str, optional
        The probe wave functions will be build and stored on this device ('cpu' or 'gpu'). The default is determined by
        the user configuration.
    aperture : BaseAperture, optional
        An optional custom aperture. The provided aperture should be a subtype of :class:`.BaseAperture`.
    aberrations : dict or Aberrations
        The phase aberrations as a dictionary.
    transforms : list of :class:`.WaveTransform`
        A list of additional wave function transforms which will be applied after creation of the probe wave functions.
    kwargs :
        Provide the aberrations as keyword arguments, forwarded to the :class:`.Aberrations`.
    """

    def __init__(
        self,
        semiangle_cutoff: float = None,
        extent: float | tuple[float, float] = None,
        gpts: int | tuple[int, int] = None,
        sampling: float | tuple[float, float] = None,
        energy: float = None,
        soft: bool = True,
        # tilt: tuple[float | BaseDistribution, float | BaseDistribution]
        # | BaseDistribution = (
        #     0.0,
        #     0.0,
        # ),
        device: str = None,
        aperture: BaseAperture = None,
        aberrations: Aberrations | dict = None,
        transforms: list[ArrayObjectTransform] = None,
        positions: BaseScan = None,
        metadata: dict = None,
        **kwargs,
    ):

        self._accelerator = Accelerator(energy=energy)

        # if not ((semiangle_cutoff is None) + (aperture is None) == 1):
        #     raise ValueError("provide exactly one of `semiangle_cutoff` or `aperture`")
        # elif semiangle_cutoff is None:
        #     semiangle_cutoff = 30.0

        if semiangle_cutoff is None and aperture is None:
            semiangle_cutoff = 30

        if aperture is None:
            aperture = Aperture(semiangle_cutoff=semiangle_cutoff, soft=soft)

        aperture._accelerator = self._accelerator

        if aberrations is None:
            aberrations = {}

        if isinstance(aberrations, dict):
            aberrations = Aberrations(energy=energy, **aberrations, **kwargs)

        aberrations._accelerator = self._accelerator

        self._aperture = aperture
        self._aberrations = aberrations

        self._grid = Grid(extent=extent, gpts=gpts, sampling=sampling)
        self._metadata = {} if metadata is None else metadata

        if transforms is None:
            transforms = CompositeArrayObjectTransform()

        if positions is None:
            positions = abtem.CustomScan([(0.0, 0.0)], squeeze=True)

        self._positions = positions
        self._transforms = transforms
        self.accelerator.match(self.aperture)

    @property
    def positions(self):
        return self._positions

    @property
    def transforms(self):
        return self._transforms

    @property
    def _ensembles(self):
        names = (
            "transforms",
            "aberrations",
            "aperture",
            "positions",
        )
        return {name : getattr(self, name) for name in names}

    @property
    def _ensemble_shapes(self):
        return tuple(ensemble.ensemble_shape for ensemble in self._ensembles.values())

    @property
    def ensemble_shape(self):
        return tuple(itertools.chain(*self._ensemble_shapes))

    @property
    def ensemble_axes_metadata(self) -> list[AxisMetadata]:
        return list(
            itertools.chain(
                *tuple(ensemble.ensemble_axes_metadata for ensemble in self._ensembles.values())
            )
        )

    def _chunk_splits(self):
        shapes = (0,) + tuple(
            len(ensemble_shape) for ensemble_shape in self._ensemble_shapes
        )
        cumulative_shapes = np.cumsum(shapes)
        return [
            (cumulative_shapes[i], cumulative_shapes[i + 1])
            for i in range(len(cumulative_shapes) - 1)
        ]

    def _arg_splits(self):
        shapes = (0,) + tuple(
            1 if len(ensemble_shape) else 0 for ensemble_shape in self._ensemble_shapes
        )
        cumulative_shapes = np.cumsum(shapes)
        return [
            (cumulative_shapes[i], cumulative_shapes[i + 1])
            for i in range(len(cumulative_shapes) - 1)
        ]

    def _partition_args(self, chunks=(1,), lazy: bool = True):
        chunks = self._validate_ensemble_chunks(chunks)

        args = ()
        for arg_split, ensemble in zip(self._chunk_splits(), self._ensembles.values()):
            arg_chunks = chunks[slice(*arg_split)]
            args += ensemble._partition_args(arg_chunks, lazy=lazy)

        return args

    @property
    def _default_ensemble_chunks(self):
        return ("auto",) * len(self.ensemble_shape)

    @classmethod
    def _from_partitioned_args_func(
        cls,
        *args,
        partials,
        arg_splits,
        **kwargs,
    ):

        args = unpack_blockwise_args(args)
        for arg_split, (name, partial) in zip(arg_splits, partials.items()):
            kwargs[name] = partial(*args[slice(*arg_split)]).item()

        new_probe = cls(**kwargs,)
        new_probe = _wrap_with_array(new_probe)
        return new_probe

    def _from_partitioned_args(self, *args, **kwargs):
        partials = {name: ensemble._from_partitioned_args() for name, ensemble in self._ensembles.items()}

        kwargs = self._copy_kwargs(
            exclude=tuple(self._ensembles.keys())
        )
        return partial(
            self._from_partitioned_args_func,
            partials=partials,
            arg_splits=self._arg_splits(),
            **kwargs,
        )

    @property
    def soft(self):
        return self.aperture.soft

    @property
    def tilt(self):
        return self

    @classmethod
    def _from_ctf(cls, ctf, **kwargs):
        return cls(
            semiangle_cutoff=ctf.semiangle_cutoff,
            soft=ctf.soft,
            aberrations=ctf.aberration_coefficients,
            **kwargs,
        )

    @property
    def ctf(self):
        """Contrast transfer function describing the probe."""
        return CTF(
            aberration_coefficients=self.aberrations.aberration_coefficients,
            semiangle_cutoff=self.semiangle_cutoff,
            energy=self.energy,
        )

    @property
    def semiangle_cutoff(self):
        """The semiangle cutoff [mrad]."""
        return self.aperture.semiangle_cutoff

    @property
    def aperture(self) -> Aperture:
        """Condenser or probe-forming aperture."""
        return self._aperture

    @aperture.setter
    def aperture(self, aperture: Aperture):
        self._aperture = aperture

    @property
    def aberrations(self) -> Aberrations:
        """Phase aberrations of the probe wave functions."""
        return self._aberrations

    @aberrations.setter
    def aberrations(self, aberrations: Aberrations):
        self._aberrations = aberrations

    @property
    def metadata(self) -> dict:
        """Metadata describing the probe wave functions."""
        return {
            **self._metadata,
            "energy": self.energy,
            **self.aperture.metadata,
            # **self._tilt.metadata,
        }

    @staticmethod
    def _build_probes(probe, wrapped: bool = True):
        if hasattr(probe, "item"):
            probe = probe.item()

        array = probe.positions._evaluate_kernel(probe)

        waves = Waves(
            array,
            energy=probe.energy,
            extent=probe.extent,
            metadata=probe.metadata,
            reciprocal_space=True,
            ensemble_axes_metadata=probe.positions.ensemble_axes_metadata,
        )

        waves = waves.apply_transform(probe.aperture)

        waves = waves.apply_transform(probe.aberrations)

        waves = waves.ensure_real_space()

        waves = waves.apply_transform(probe.transforms)

        if not wrapped:
            waves = waves.array

        return waves

    @staticmethod
    def _lazy_build_probes(probe, max_batch):
        if isinstance(max_batch, int):
            max_batch = int(max_batch * np.prod(probe.gpts))

        chunks = probe._default_ensemble_chunks + probe.gpts

        chunks = validate_chunks(
            shape=probe.ensemble_shape + probe.gpts,
            chunks=chunks + (-1, -1),
            limit=max_batch,
            dtype=probe.dtype,
        )

        blocks = probe.ensemble_blocks(chunks=chunks[:-2])

        xp = get_array_module(probe.device)

        array = blocks.map_blocks(
            probe._build_probes,
            meta=xp.array((), dtype=np.complex64),
            new_axis=tuple_range(2, len(probe.ensemble_shape)),
            chunks=blocks.chunks + probe.gpts,
            wrapped=False,
            enforce_ndim=True,
        )

        return Waves(
            array,
            energy=probe.energy,
            extent=probe.extent,
            reciprocal_space=False,
            metadata=probe.metadata,
            ensemble_axes_metadata=probe.ensemble_axes_metadata,
        )

    def build(
        self,
        scan: Sequence | BaseScan = None,
        max_batch: int | str = "auto",
        lazy: bool = None,
    ) -> Waves:
        """
        Build probe wave functions at the provided positions.

        Parameters
        ----------
        scan : array of `xy`-positions or BaseScan, optional
            Positions of the probe wave functions. If not given, scans across the entire potential at Nyquist sampling.
        max_batch : int, optional
            The number of wave functions in each chunk of the Dask array. If 'auto' (default), the batch size is
            automatically chosen based on the abtem user configuration settings "dask.chunk-size" and
            "dask.chunk-size-gpu".
        lazy : bool, optional
            If True, create the wave functions lazily, otherwise, calculate instantly. If not given, defaults to the
            setting in the user configuration file.

        Returns
        -------
        probe_wave_functions : Waves
            The built probe wave functions.
        """
        scan = _validate_scan(scan, self)

        probe = self.copy()

        probe._positions = scan

        if not lazy:
            return self._build_probes(probe)

        return self._lazy_build_probes(probe, max_batch)

    def multislice(
        self,
        potential: BasePotential | Atoms,
        scan: tuple | BaseScan = None,
        detectors: BaseDetector = None,
        max_batch: int | str = "auto",
        lazy: bool = None,
    ) -> BaseMeasurements | Waves | list[BaseMeasurements | Waves]:
        """
        Run the multislice algorithm for probe wave functions at the provided positions.

        Parameters
        ----------
        potential : BasePotential or Atoms
            The scattering potential. Optionally atoms can be directly given.
        scan : array of xy-positions or BaseScan, optional
            Positions of the probe wave functions. If not given, scans across the entire potential at Nyquist sampling.
        detectors : BaseDetector or list of BaseDetector, optional
            A detector or a list of detectors defining how the wave functions should be converted to measurements after
            running the multislice algorithm. If not given, defaults to the flexible annular detector.
        max_batch : int, optional
            The number of wave functions in each chunk of the Dask array. If 'auto' (default), the batch size is
            automatically chosen based on the abtem user configuration settings "dask.chunk-size" and
            "dask.chunk-size-gpu".
        lazy : bool, optional
            If True, create the wave functions lazily, otherwise, calculate instantly. If None, this defaults to the
            setting in the user configuration file.
        transition_potentials : BaseTransitionPotential, optional
            Used to describe inelastic core losses.

        Returns
        -------
        measurements : BaseMeasurements or Waves or list of BaseMeasurement
        """

        probe = self.copy()

        probe.grid.match(potential)

        scan = _validate_scan(scan, probe)

        probe._positions = scan

        if not lazy:
            probes = self._build_probes(probe)
        else:
            probes = self._lazy_build_probes(probe, max_batch)

        multislice = MultisliceTransform(potential, detectors)

        measurements = probes.apply_transform(multislice)

        return measurements

    def scan(
        self,
        potential: Atoms | BasePotential,
        scan: BaseScan | np.ndarray | Sequence = None,
        detectors: BaseDetector | Sequence[BaseDetector] = None,
        max_batch: int | str = "auto",
        transition_potentials=None,
        lazy: bool = None,
    ) -> BaseMeasurements | Waves | list[BaseMeasurements | Waves]:
        """
        Run the multislice algorithm from probe wave functions over the provided scan.

        Parameters
        ----------
        potential : BasePotential or Atoms
            The scattering potential.
        scan : BaseScan
            Positions of the probe wave functions. If not given, scans across the entire potential at Nyquist sampling.
        detectors : BaseDetector, list of BaseDetector, optional
            A detector or a list of detectors defining how the wave functions should be converted to measurements after
            running the multislice algorithm. See abtem.measurements.detect for a list of implemented detectors.
        max_batch : int, optional
            The number of wave functions in each chunk of the Dask array. If 'auto' (default), the batch size is
            automatically chosen based on the abtem user configuration settings "dask.chunk-size" and
            "dask.chunk-size-gpu".
        lazy : bool, optional
            If True, create the measurements lazily, otherwise, calculate instantly. If None, this defaults to the value
            set in the configuration file.

        Returns
        -------
        detected_waves : BaseMeasurements or list of BaseMeasurement
            The detected measurement (if detector(s) given).
        exit_waves : Waves
            Wave functions at the exit plane(s) of the potential (if no detector(s) given).
        """

        if scan is None:
            scan = GridScan()

        if detectors is None:
            detectors = FlexibleAnnularDetector()

        measurements = self.multislice(
            scan=scan,
            potential=potential,
            detectors=detectors,
            lazy=lazy,
            max_batch=max_batch,
        )

        return measurements

    def profiles(self, angle: float = 0.0) -> RealSpaceLineProfiles:
        """
        Create a line profile through the center of the probe.

        Parameters
        ----------
        angle : float, optional
            Angle with respect to the `x`-axis of the line profile [degree].
        """

        def _line_intersect_rectangle(point0, point1, lower_corner, upper_corner):
            if point0[0] == point1[0]:
                return (point0[0], lower_corner[1]), (point0[0], upper_corner[1])

            m = (point1[1] - point0[1]) / (point1[0] - point0[0])

            def y(x):
                return m * (x - point0[0]) + point0[1]

            def x(y):
                return (y - point0[1]) / m + point0[0]

            if y(0) < lower_corner[1]:
                intersect0 = (x(lower_corner[1]), y(x(lower_corner[1])))
            else:
                intersect0 = (0, y(lower_corner[0]))

            if y(upper_corner[0]) > upper_corner[1]:
                intersect1 = (x(upper_corner[1]), y(x(upper_corner[1])))
            else:
                intersect1 = (upper_corner[0], y(upper_corner[0]))

            return intersect0, intersect1

        point1 = (self.extent[0] / 2, self.extent[1] / 2)

        measurement = self.build(point1).intensity()

        point2 = point1 + np.array(
            [np.cos(np.pi * angle / 180), np.sin(np.pi * angle / 180)]
        )
        point1, point2 = _line_intersect_rectangle(
            point1, point2, (0.0, 0.0), self.extent
        )
        return measurement.interpolate_line(point1, point2)

    def show(self, complex_images: bool = False, **kwargs):
        """
        Show the intensity of the probe wave function.

        Parameters
        ----------
        kwargs : Keyword arguments for the :func:`.Images.show` function.
        """
        wave = self.build((self.extent[0] / 2, self.extent[1] / 2))
        if complex_images:
            images = wave.complex_images()
        else:
            images = wave.intensity()
        return images.show(**kwargs)
