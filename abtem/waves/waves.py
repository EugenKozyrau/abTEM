"""Module to describe electron waves and their propagation."""
from collections import Iterable
from copy import copy
from typing import Union, Sequence, Tuple, List, Dict

import dask
import dask.array as da
import numpy as np
import zarr
from ase import Atoms

from abtem.core.antialias import AntialiasAperture
from abtem.core.axes import HasAxesMetadata, frozen_phonons_axes_metadata
from abtem.core.backend import get_array_module, _validate_device
from abtem.core.complex import abs2
from abtem.core.dask import computable, HasDaskArray, BuildsDaskArray, ComputableList, _validate_lazy
from abtem.core.energy import Accelerator
from abtem.core.fft import fft2, ifft2, fft2_convolve, fft_crop, fft2_interpolate, fft_shift_kernel
from abtem.core.grid import Grid
from abtem.measure.detect import AbstractDetector
from abtem.measure.measure import DiffractionPatterns, Images, AbstractMeasurement
from abtem.potentials.potentials import Potential, AbstractPotential
from abtem.waves.base import WavesLikeMixin, AbstractScannedWaves
from abtem.waves.multislice import multislice
from abtem.waves.scan import AbstractScan
from abtem.waves.tilt import BeamTilt
from abtem.waves.transfer import CTF


class Waves(HasDaskArray, WavesLikeMixin, HasAxesMetadata):
    """
    Waves object

    The waves object can define a batch of arbitrary 2D wave functions defined by a complex numpy array.

    Parameters
    ----------
    extent : one or two float
        Lateral extent of wave function [Å].
    sampling : one or two float
        Lateral sampling of wave functions [1 / Å].
    energy : float
        Electron energy [eV].
    tilt : two float
        Small angle beam tilt [mrad].
    antialias_aperture : float
        Assumed antialiasing aperture as a fraction of the real space Nyquist frequency. Default is 2/3.
    """

    def __init__(self,
                 array: np.ndarray,
                 energy: float = None,
                 extent: Union[float, Tuple[float, float]] = None,
                 sampling: Union[float, Tuple[float, float]] = None,
                 tilt: Tuple[float, float] = (0., 0.),
                 antialias_aperture: float = 2 / 3.,
                 extra_axes_metadata: List[Dict] = None,
                 metadata: Dict = None):

        if len(array.shape) < 2:
            raise RuntimeError('Wave function array should be have 2 dimensions or more')

        self._grid = Grid(extent=extent, gpts=array.shape[-2:], sampling=sampling, lock_gpts=True)
        self._accelerator = Accelerator(energy=energy)
        self._beam_tilt = BeamTilt(tilt=tilt)
        self._antialias_aperture = AntialiasAperture(cutoff=antialias_aperture)

        super().__init__(array=array)

        self._extra_axes_metadata = self._validate_extra_axes_metadata(extra_axes_metadata)

        if metadata is None:
            metadata = {}

        self._metadata = metadata

    def lazy(self):
        self._array = da.from_array(self.array)

    def squeeze(self) -> 'Waves':
        shape = self.shape[:-2]
        squeezed = tuple(np.where([n == 1 for n in shape])[0])
        self._extra_axes_metadata = self._remove_axes_metadata(squeezed)
        self._array = np.squeeze(self.array, axis=squeezed)
        return self

    @property
    def metadata(self) -> Dict:
        return self._metadata

    @property
    def num_axes(self) -> int:
        return len(self.array.shape)

    @property
    def extra_axes_metadata(self) -> List[Dict]:
        return self._extra_axes_metadata

    def __len__(self) -> int:
        return len(self.array)

    @property
    def shape(self) -> Tuple[int, int]:
        return self.array.shape

    @computable
    def intensity(self) -> Images:
        """
        Calculate the intensity of the wave functions at the image plane.

        Returns
        -------
        Measurement
            The wave function intensity.
        """
        return Images(abs2(self.array), sampling=self.sampling, axes_metadata=self.extra_axes_metadata)

    def downsample(self, max_angle: str = 'valid') -> 'Waves':
        xp = get_array_module(self.array)
        gpts = self._gpts_within_angle(max_angle)

        if self.is_lazy:
            array = self.array.map_blocks(fft2_interpolate, new_shape=gpts,
                                          chunks=self.array.chunks[:-2] + gpts,
                                          meta=xp.array((), dtype=xp.complex64))
        else:
            array = fft2_interpolate(self.array, new_shape=gpts)

        antialias_aperture = self.antialias_aperture * min(self.gpts[0] / gpts[0], self.gpts[1] / gpts[1])

        return Waves(array, extent=self.extent, energy=self.energy, antialias_aperture=antialias_aperture,
                     extra_axes_metadata=self.extra_axes_metadata)

    def detect(self, detectors: Union[AbstractDetector, List[AbstractDetector]]) \
            -> Union[AbstractMeasurement, List[AbstractMeasurement]]:

        if not isinstance(detectors, Iterable):
            detectors = (detectors,)

        measurements = []
        for detector in detectors:
            measurements += [detector.detect(self)]

        if len(measurements) == 1:
            return measurements[0]

        return measurements

    def diffraction_patterns(self, max_angle: str = 'valid', block_direct: bool = False,
                             fftshift: bool = True) -> DiffractionPatterns:
        """
        Calculate the intensity of the wave functions at the diffraction plane.

        Returns
        -------
        Measurement object
            The intensity of the diffraction pattern(s).
        """

        def _diffraction_pattern(array, new_gpts, fftshift):
            xp = get_array_module(array)

            array = fft2(array, overwrite_x=False)

            if array.shape[-2:] != new_gpts:
                array = fft_crop(array, new_shape=array.shape[:-2] + new_gpts)

            array = abs2(array)

            if fftshift:
                return xp.fft.fftshift(array, axes=(-1, -2))

            return array

        xp = get_array_module(self.array)
        new_gpts = self._gpts_within_angle(max_angle)

        if self.is_lazy:
            pattern = self.array.map_blocks(_diffraction_pattern, new_gpts=new_gpts, fftshift=fftshift,
                                            chunks=self.array.chunks[:-2] + ((new_gpts[0],), (new_gpts[1],)),
                                            meta=xp.array((), dtype=xp.float32))
        else:
            pattern = _diffraction_pattern(self.array, new_gpts=new_gpts, fftshift=fftshift)

        diffraction_patterns = DiffractionPatterns(pattern, angular_sampling=self.angular_sampling, fftshift=fftshift,
                                                   axes_metadata=self.extra_axes_metadata)

        if block_direct:
            diffraction_patterns = diffraction_patterns.block_direct(radius=block_direct)

        return diffraction_patterns

    def apply_ctf(self, ctf: CTF = None, in_place: bool = False, **kwargs) -> 'Waves':
        """
        Apply the aberrations defined by a CTF object to wave function.

        Parameters
        ----------
        ctf : CTF
            Contrast Transfer Function object to be applied.
        kwargs :
            Provide the parameters of the contrast transfer function as keyword arguments. See the documentation for the
            CTF object.

        Returns
        -------
        Waves object
            The wave functions with aberrations applied.
        """

        if ctf is None:
            ctf = CTF(**kwargs)

        if not ctf.accelerator.energy:
            ctf.accelerator.match(self.accelerator)

        self.accelerator.match(ctf.accelerator, check_match=True)

        self.accelerator.check_is_defined()
        self.grid.check_is_defined()

        kernel = ctf.evaluate_on_grid(extent=self.extent, gpts=self.gpts, sampling=self.sampling)

        xp = get_array_module(self.array)

        kernel = xp.asarray(kernel)

        return self.__class__(fft2_convolve(self.array, kernel, overwrite_x=in_place),
                              extent=self.extent,
                              energy=self.energy,
                              extra_axes_metadata=self.extra_axes_metadata,
                              tilt=self.tilt)

    def multislice(self, potential: AbstractPotential) -> 'Waves':
        """
        Propagate and transmit wave function through the provided potential.

        Parameters
        ----------
        potential : Potential
            The potential through which to propagate the wave function.

        Returns
        -------
        Waves object
            Wave function at the exit plane of the potential.
        """

        self.grid.match(potential)
        self.grid.check_is_defined()
        self.accelerator.check_is_defined()

        potential = self._validate_potential(potential)

        exit_waves = []
        for p in potential.get_potential_configurations(lazy=self.is_lazy):
            exit_waves.append(multislice(self.copy(), p))

        if len(exit_waves) > 1:
            if self.is_lazy:
                array = da.stack([exit_wave.array for exit_wave in exit_waves], axis=0)
            else:
                array = np.stack([exit_wave.array for exit_wave in exit_waves], axis=0)

            extra_axes_metadata = [frozen_phonons_axes_metadata] + self.extra_axes_metadata

            exit_waves = self.__class__(array=array,
                                        extent=self.extent, energy=self.energy, tilt=self.tilt,
                                        antialias_aperture=2 / 3., extra_axes_metadata=extra_axes_metadata,
                                        metadata=self._metadata)
        else:
            exit_waves = exit_waves[0]

        return exit_waves

    def to_zarr(self, url: str, overwrite: bool = False):
        """
        Write potential to a zarr file.

        Parameters
        ----------
        url: str
            url to which the data is saved.
        """

        with zarr.open(url, mode='w') as root:
            if not self.is_lazy:
                self.lazy()

            self.array.to_zarr(url, component='array', overwrite=overwrite)
            root.attrs['energy'] = self.energy
            root.attrs['extent'] = self.extent
            root.attrs['tilt'] = self.tilt
            root.attrs['antialias_aperture'] = self.antialias_aperture
            root.attrs['extra_axes_metadata'] = self.extra_axes_metadata
            root.attrs['metadata'] = self.metadata

    @classmethod
    def from_zarr(cls, url: str, chunks: int = None) -> 'Waves':
        """
        Read wave functions from a hdf5 file.

        path : str
            The path to read the file.
        """

        with zarr.open(url, mode='r') as f:
            energy = f.attrs['energy']
            extent = f.attrs['extent']
            tilt = f.attrs['tilt']
            antialias_aperture = f.attrs['antialias_aperture']
            extra_axes_metadata = f.attrs['extra_axes_metadata']
            metadata = f.attrs['metadata']
            shape = f['array'].shape

        if chunks is None:
            chunks = (-1,) * (len(shape) - 2)

        array = da.from_zarr(url, component='array', chunks=chunks + (-1, -1))
        return cls(array=array, energy=energy, extent=extent, tilt=tilt, antialias_aperture=antialias_aperture,
                   extra_axes_metadata=extra_axes_metadata)

    def __getitem__(self, item) -> 'Waves':
        if len(self.array.shape) <= self.grid.dimensions:
            raise RuntimeError()
        return self.__class__(array=self._array[item], extent=self.extent, energy=self.energy)

    def show(self, ax=None, **kwargs):
        """
        Show the wave function.

        kwargs :
            Additional keyword arguments for the abtem.plot.show_image function.
        """
        return self.intensity().show(ax=ax, **kwargs)

    def copy(self, copy_array: bool = True) -> 'Waves':
        array = self.array
        if copy_array:
            array = array.copy()
        new_copy = self.__class__(array=array,
                                  tilt=self.tilt,
                                  energy=self.energy,
                                  extent=self.extent,
                                  antialias_aperture=self.antialias_aperture,
                                  extra_axes_metadata=copy(self._extra_axes_metadata))

        return new_copy


class PlaneWave(WavesLikeMixin):
    """
    Plane wave object

    The plane wave object is used for building plane waves.

    Parameters
    ----------
    extent : two float
        Lateral extent of wave function [Å].
    gpts : two int
        Number of grid points describing the wave function.
    sampling : two float
        Lateral sampling of wave functions [1 / Å].
    energy : float
        Electron energy [eV].
    tilt : two floats
        Small angle beam tilt [mrad].
    device : str
        The plane waves will be build on this device.
    """

    def __init__(self,
                 extent: Union[float, Tuple[float, float]] = None,
                 gpts: Union[int, Tuple[int, int]] = None,
                 sampling: Union[float, Tuple[float, float]] = None,
                 energy: float = None,
                 tilt: Tuple[float, float] = None,
                 device: str = 'cpu'):
        self._grid = Grid(extent=extent, gpts=gpts, sampling=sampling)
        self._accelerator = Accelerator(energy=energy)
        self._beam_tilt = BeamTilt(tilt=tilt)
        self._antialias_aperture = AntialiasAperture()
        self._device = _validate_device(device)

    def multislice(self, potential: Union[AbstractPotential, Atoms], lazy: bool = None) -> Waves:
        """
        Build plane wave function and propagate it through the potential. The grid of the two will be matched.

        Parameters
        ----------
        potential : Potential or Atoms object
            The potential through which to propagate the wave function.
        lazy : bool, optional
            Return lazy computation. Default is True.

        Returns
        -------
        Waves object
            Wave function at the exit plane of the potential.
        """

        if isinstance(potential, Atoms):
            potential = Potential(atoms=potential)

        potential.grid.match(self)

        waves = self.build(lazy=True).multislice(potential)

        if not _validate_lazy(lazy):
            waves.compute()

        return waves

    def build(self, lazy: bool = None) -> Waves:
        """Build the plane wave function as a Waves object."""
        xp = get_array_module(self._device)
        self.grid.check_is_defined()

        def plane_wave(gpts, xp):
            return xp.ones(gpts, dtype=xp.complex64)

        if _validate_lazy(lazy):
            array = dask.delayed(plane_wave)(self.gpts, xp)
            array = da.from_delayed(array, shape=self.gpts, meta=xp.array((), dtype=xp.complex64))
        else:
            array = plane_wave(self.gpts, xp)

        return Waves(array, extent=self.extent, energy=self.energy)

    def __copy__(self) -> 'PlaneWave':
        return self.__class__(extent=self.extent, gpts=self.gpts, sampling=self.sampling, energy=self.energy,
                              device=self._device)


class Probe(AbstractScannedWaves, BuildsDaskArray):
    """
    Probe wavefunction object

    The probe object can represent a stack of electron probe wavefunctions for simulating scanning transmission
    electron microscopy.

    See the docs of abtem.transfer.CTF for a description of the parameters related to the contrast transfer function.

    Parameters
    ----------
    extent : two float, optional
        Lateral extent of wave functions [Å].
    gpts : two int, optional
        Number of grid points describing the wave functions.
    sampling : two float, optional
        Lateral sampling of wave functions [1 / Å].
    energy : float, optional
        Electron energy [eV].
    ctf : CTF
        Contrast transfer function object. Note that this can be specified
    device : str
        The probe wave functions will be build on this device.
    kwargs :
        Provide the parameters of the contrast transfer function as keyword arguments. See the documentation for the
        CTF object.
    """

    def __init__(self,
                 extent: Union[float, Tuple[float, float]] = None,
                 gpts: Union[int, Tuple[int, int]] = None,
                 sampling: Union[float, Tuple[float, float]] = None,
                 energy: float = None,
                 ctf: CTF = None,
                 tilt: Tuple[float, float] = None,
                 device: str = None,
                 **kwargs):

        if ctf is None:
            ctf = CTF(energy=energy, **kwargs)

        if ctf.energy is None:
            ctf.energy = energy

        if ctf.energy != energy:
            raise RuntimeError('CTF energy does match probe energy')

        self._ctf = ctf
        self._accelerator = self._ctf._accelerator
        self._grid = Grid(extent=extent, gpts=gpts, sampling=sampling)
        self._antialias_aperture = AntialiasAperture()
        self._beam_tilt = BeamTilt(tilt=tilt)
        self._device = _validate_device(device)

    @property
    def ctf(self) -> CTF:
        """Probe contrast transfer function."""
        return self._ctf

    def _validate_positions(self,
                            positions: Union[Sequence, AbstractScan] = None,
                            lazy: bool = True,
                            chunks: Union[int, str] = 'auto'):

        lazy = _validate_lazy(lazy)

        chunks = self._validate_chunks(chunks)

        if hasattr(positions, 'get_positions'):
            return positions.get_positions(lazy=lazy, chunks=chunks), positions.axes_metadata

        if positions is None:
            positions = (self.extent[0] / 2, self.extent[1] / 2)

        if not isinstance(positions, da.core.Array):
            positions = np.array(positions, dtype=np.float32)

        if isinstance(positions, np.ndarray) and lazy:
            positions = da.from_array(positions)

        if len(positions.shape) == 1:
            positions = positions[None]

        if positions.shape[-1] != 2:
            raise ValueError()

        return positions, [{'type': 'positions'}] * (len(positions.shape) - 1)

    def build(self,
              positions: Union[AbstractScan, Sequence] = None,
              chunks: Union[int, str] = 'auto',
              lazy: bool = None) -> Waves:

        """
        Build probe wave functions at the provided positions.

        Parameters
        ----------
        positions : array of xy-positions
            Positions of the probe wave functions

        Returns
        -------
        Waves object
            Probe wave functions as a Waves object.
        """

        self.grid.check_is_defined()
        self.accelerator.check_is_defined()

        positions, extra_axes_metadata = self._validate_positions(positions, lazy=lazy, chunks=chunks)

        xp = get_array_module(self._device)

        def _evaluate_probe(positions):
            xp = get_array_module(positions)
            positions = positions / xp.array(self.sampling).astype(np.float32)
            array = fft_shift_kernel(positions, shape=self.gpts)
            array *= self._ctf.evaluate_on_grid(gpts=self.gpts, sampling=self.sampling, xp=xp)
            array /= xp.sqrt(abs2(array).sum((-2, -1), keepdims=True))
            return array

        def calculate_probes(positions, xp):
            positions = xp.asarray(positions)
            return ifft2(_evaluate_probe(positions))

        if isinstance(positions, da.core.Array):
            drop_axis = len(positions.shape) - 1
            new_axis = (len(positions.shape) - 1, len(positions.shape))
            array = positions.map_blocks(calculate_probes,
                                         xp=xp,
                                         meta=xp.array((), dtype=np.complex64),
                                         drop_axis=drop_axis, new_axis=new_axis,
                                         chunks=positions.chunks[:-1] + ((self.gpts[0],), (self.gpts[1],)))

        else:
            array = calculate_probes(positions, xp)

        return Waves(array, extent=self.extent,
                     energy=self.energy, tilt=self.tilt, extra_axes_metadata=extra_axes_metadata)

    def multislice(self,
                   potential: Union[AbstractPotential],
                   positions: Union[AbstractScan, Sequence] = None,
                   chunks: Union[int, str] = 'auto',
                   lazy: bool = None) -> Waves:
        """
        Build probe wave functions at the provided positions and propagate them through the potential.

        Parameters
        ----------
        positions : array of xy-positions
            Positions of the probe wave functions.
        potential : Potential or Atoms object
            The scattering potential.
        pbar : bool, optional
            Display progress bars. Default is True.

        Returns
        -------
        Waves object
            Probe exit wave functions as a Waves object.
        """

        if hasattr(potential, 'grid'):
            self.grid.match(potential.grid)

        self.grid.check_is_defined()
        self.accelerator.check_is_defined()

        if hasattr(positions, 'match'):
            positions.match(self)

        positions, extra_axes_metadata = self._validate_positions(positions, lazy=True, chunks=chunks)

        xp = get_array_module(self._device)

        def build_probes_multislice(positions, potential):
            waves = self.build(positions, lazy=False)
            waves = waves.multislice(potential)
            return waves.array

        def multislice_iteration(slice_iterator):
            if isinstance(positions, da.core.Array):
                array = positions.map_blocks(build_probes_multislice,
                                             potential=slice_iterator,
                                             meta=xp.array((), dtype=np.complex64),
                                             drop_axis=len(positions.shape) - 1,
                                             new_axis=(len(positions.shape) - 1, len(positions.shape)),
                                             chunks=positions.chunks[:-1] +
                                                    ((self.gpts[0],), (self.gpts[1],)))

            else:
                array = build_probes_multislice(positions, slice_iterator)

            return array

        if hasattr(potential, 'get_chunk'):
            array = multislice_iteration(potential)

        elif hasattr(potential, 'get_projected_potentials'):
            exit_waves_arrays = []

            for projected_potential in potential.get_projected_potentials(lazy=True):
                exit_waves_arrays.append(multislice_iteration(projected_potential))

            array = da.stack([array for array in exit_waves_arrays], axis=0)
            extra_axes_metadata = [frozen_phonons_axes_metadata] + extra_axes_metadata
        else:
            raise RuntimeError()

        waves = Waves(array, extent=self.extent, energy=self.energy, tilt=self.tilt,
                      extra_axes_metadata=extra_axes_metadata)

        if not _validate_lazy(lazy):
            waves.compute()

        return waves

    def scan(self,
             positions: Union[AbstractScan, np.ndarray, Sequence],
             detectors: Union[AbstractDetector, Sequence[AbstractDetector]],
             potential: Union[AbstractPotential],
             chunks: Union[int, str] = 'auto',
             lazy: bool = None) -> Union[List, AbstractMeasurement]:

        """
        Raster scan the probe across the potential and record a measurement for each detector.

        Parameters
        ----------
        positions : Scan object or nx2 np.ndarray or
            Scan object defining the positions of the probe wave functions.
        detectors : Detector or list of detectors
            The detectors recording the measurements.
        potential : Potential
            The potential to scan the probe over.
        max_batch : int, optional
            The probe batch size. Larger batches are faster, but require more memory. Default is None.
        pbar : bool, optional
            Display progress bars. Default is True.

        Returns
        -------

        """
        self.grid.match(potential.grid)
        self.grid.check_is_defined()
        self.accelerator.check_is_defined()

        if hasattr(positions, 'match'):
            positions.match(self)

        validated_positions, axes_metadata = self._validate_positions(positions, lazy=True, chunks=chunks)
        detectors = self._validate_detectors(detectors)

        def build_probes_multislice_detect(positions, potential, detectors):
            waves = self.build(positions, lazy=False)
            waves = waves.multislice(potential)
            measurements = waves.detect(detectors)
            if isinstance(measurements, (tuple, list)):
                return tuple(measurement.array for measurement in measurements)
            else:
                return measurements.array

        def gufunc_signature_and_output_size(detectors, waves):
            first_new_index = 1
            signatures = []
            output_sizes = {}
            for detector in detectors:
                shape = detector.detected_shape(waves)
                indices = range(first_new_index, first_new_index + len(shape))
                signatures.append(f'({",".join([str(i) for i in indices])})')
                output_sizes.update({str(index): n for index, n in zip(indices, shape)})
                first_new_index = first_new_index + len(shape)

            signature = '(0)->' + ','.join(signatures)
            return signature, output_sizes

        def multislice_iteration(slice_iterator):

            if isinstance(validated_positions, da.core.Array):
                signature, output_sizes = gufunc_signature_and_output_size(detectors, self)
                dtypes = list([detector.detected_dtype for detector in detectors])

                return da.apply_gufunc(build_probes_multislice_detect,
                                       signature,
                                       validated_positions,
                                       output_dtypes=dtypes,
                                       output_sizes=output_sizes,
                                       potential=slice_iterator,
                                       detectors=detectors,
                                       )

            else:
                return build_probes_multislice_detect(validated_positions, slice_iterator, detectors)

        measurement_arrays = []
        for p in potential.get_potential_configurations(lazy=True):
            measurement_arrays.append(multislice_iteration(p))

        if isinstance(measurement_arrays[0], tuple):
            measurement_arrays = list(map(da.stack, map(list, zip(*measurement_arrays))))
        else:
            measurement_arrays = [da.stack(measurement_arrays)]

        measurements = []
        for detector, measurement_array in zip(detectors, measurement_arrays):
            if detector.ensemble_mean:
                measurement_array = measurement_array.mean(0)
                axes_metadata = []
            else:
                axes_metadata = frozen_phonons_axes_metadata

            measurements.append(detector.measurement_from_array(measurement_array, scan=positions, waves=self,
                                                                axes_metadata=axes_metadata))

        if len(measurements) == 1:
            output = measurements[0]
        else:
            output = ComputableList(measurements)

        if not _validate_lazy(lazy):
            output.compute()

        return output

    def profile(self, angle: float = 0.):
        self.grid.check_is_defined()

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

        point1 = np.array((self.extent[0] / 2, self.extent[1] / 2))

        measurement = self.build(point1).intensity()

        point2 = point1 + np.array([np.cos(np.pi * angle / 180), np.sin(np.pi * angle / 180)])
        point1, point2 = _line_intersect_rectangle(point1, point2, (0., 0.), self.extent)
        return measurement.interpolate_line(point1, point2)

    def __copy__(self) -> 'Probe':
        return self.__class__(gpts=self.gpts,
                              extent=self.extent,
                              sampling=self.sampling,
                              energy=self.energy,
                              tilt=self.tilt,
                              ctf=self.ctf.copy())

    def show(self, **kwargs):
        """
        Show the probe wave function.

        Parameters
        ----------
        angle : float, optional
            Angle along which the profile is shown [deg]. Default is 0 degrees.
        kwargs : Additional keyword arguments for the abtem.plot.show_image function.
        """
        self.grid.check_is_defined()
        return self.build((self.extent[0] / 2, self.extent[1] / 2)).intensity().show(**kwargs)