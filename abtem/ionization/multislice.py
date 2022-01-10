from typing import TYPE_CHECKING

import numpy as np
from ase import Atoms

from abtem.core.antialias import AntialiasAperture
from abtem.core.backend import get_array_module
from abtem.core.complex import complex_exponential
from abtem.measure.measure import Images
from abtem.potentials.potentials import validate_potential
from abtem.structures.slicing import SliceIndexedAtoms
from abtem.waves.multislice import FresnelPropagator, multislice

if TYPE_CHECKING:
    from abtem.waves.prism import SMatrix


def validate_sites(potential=None, sites=None):
    if sites is None:
        if hasattr(potential, 'atoms'):
            sites = potential.atoms
        else:
            raise RuntimeError(f'transition sites cannot be inferred from potential of type {type(potential)}')

    if isinstance(sites, SliceIndexedAtoms):
        if len(potential) != len(sites):
            raise RuntimeError(f'transition sites slices({len(sites)}) != potential slices({len(potential)})')
    elif isinstance(sites, Atoms):
        sites = SliceIndexedAtoms(sites, slice_thickness=potential.slice_thickness)

    else:
        raise RuntimeError(f'transition sites must be Atoms or SliceIndexedAtoms, received {type(sites)}')

    return sites


def transition_potential_multislice(waves,
                                    potential,
                                    detectors,
                                    transition_potentials,
                                    sites=None,
                                    ctf=None,
                                    scan=None):
    if hasattr(waves, 'reduce') and scan is None:
        raise RuntimeError()

    if not hasattr(waves, 'reduce') and scan is not None:
        raise RuntimeError()

    potential = validate_potential(potential)
    sites = validate_sites(potential, sites)

    transition_potentials.grid.match(waves)
    transition_potentials.accelerator.match(waves)

    antialias_aperture = AntialiasAperture(device=get_array_module(waves.array)).match_grid(waves)
    propagator = FresnelPropagator(device=get_array_module(waves.array)).match_waves(waves)

    transmission_function = potential.build().transmission_function(energy=waves.energy)
    transmission_function = antialias_aperture.bandlimit(transmission_function)

    measurements = [detector.allocate_measurement(waves, scan=scan) for detector in detectors]
    for i, (transmission_function_slice, sites_slice) in enumerate(zip(transmission_function, sites)):
        sites_slice = transition_potentials.validate_sites(sites_slice)

        for _, scattered_waves in transition_potentials.generate_scattered_waves(waves, sites_slice):
            scattered_waves = multislice(scattered_waves,
                                         transmission_function,
                                         start=i,
                                         propagator=propagator,
                                         antialias_aperture=antialias_aperture)

            if ctf is not None:
                scattered_waves = scattered_waves.apply_ctf(ctf)

            for j, detector in enumerate(detectors):
                if hasattr(scattered_waves, 'reduce'):
                    measurement = scattered_waves.reduce(positions=scan, detectors=detector).sum(0)
                else:
                    measurement = detector.detect(scattered_waves).sum(0)

                measurements[j].add(measurement)

        propagator.thickness = transmission_function_slice.thickness
        waves = transmission_function_slice.transmit(waves)
        waves = propagator.propagate(waves)

    return measurements


def linear_scaling_transition_multislice(S1: 'SMatrix', S2: 'SMatrix', scan, transition_potentials):
    xp = get_array_module(S1._device)
    from tqdm.auto import tqdm

    positions = scan.get_positions(lazy=False).reshape((-1, 2))

    prism_region = (S1.extent[0] / S1.interpolation[0] / 2, S1.extent[1] / S1.interpolation[1] / 2)

    positions = xp.asarray(positions, dtype=np.float32)
    coefficients = complex_exponential(-2. * np.float32(np.pi) * positions[:, 0, None] * S1.wave_vectors[None, :, 0])
    coefficients *= complex_exponential(-2. * np.float32(np.pi) * positions[:, 1, None] * S1.wave_vectors[None, :, 1])
    coefficients = coefficients / xp.sqrt(coefficients.shape[1]).astype(np.float32)

    potential = S1.potential

    sites = validate_sites(potential, sites=None)
    chunks = S1.chunks
    stream = S1._device == 'gpu' and S1._store_on_host

    S1 = S1.build(lazy=False, stop=0, normalization='planewaves')
    S2 = S2.build(lazy=False, start=len(potential), stop=0, normalization='planewaves')

    images = xp.zeros(len(positions), dtype=np.float32)
    for i in tqdm(range(len(potential))):

        if stream:
            S1 = S1.streaming_multislice(potential, chunks=chunks, start=max(i - 1, 0), stop=i)
            S2 = S2.streaming_multislice(potential, chunks=chunks, start=max(i - 1, 0), stop=i)
        else:
            S1 = S1.multislice(potential, start=max(i - 1, 0), stop=i)
            S2 = S2.multislice(potential, start=max(i - 1, 0), stop=i)

        sites_slice = transition_potentials.validate_sites(sites[i])

        for site in sites_slice:
            S2_crop = S2.crop_to_positions(site)
            scattered_S1 = S1.crop_to_positions(site)

            if stream:
                S2_crop = S2_crop.copy('gpu')
                scattered_S1 = scattered_S1.copy('gpu')

            shifted_site = site - np.array(scattered_S1.crop_offset) * np.array(scattered_S1.sampling)
            scattered_S1 = transition_potentials.scatter(scattered_S1, shifted_site)

            if S1.interpolation == (1, 1):
                cropped_coefficients = coefficients
                mask = None
            else:
                mask = xp.ones(len(coefficients), dtype=bool)
                if S1.interpolation[0] > 1:
                    mask *= (xp.abs(positions[:, 0] - site[0]) % (S1.extent[0] - prism_region[0])) <= prism_region[0]
                if S1.interpolation[1] > 1:
                    mask *= (xp.abs(positions[:, 1] - site[1]) % (S1.extent[1] - prism_region[1])) <= prism_region[1]

                cropped_coefficients = coefficients[mask]

            SHn0 = xp.dot(S2_crop.array.reshape((1, len(S2), -1)),
                          xp.swapaxes(scattered_S1.array.reshape((len(scattered_S1.array), len(S1), -1)), 1, 2))

            SHn0 = xp.swapaxes(SHn0[0], 0, 1)

            if mask is not None:
                images[mask] += (xp.abs(xp.dot(SHn0, cropped_coefficients.T[None])) ** 2).sum((0, 1, 2))
            else:
                images += (xp.abs(xp.dot(SHn0, cropped_coefficients.T[None])) ** 2).sum((0, 1, 2))

    images *= np.prod(S1.interpolation).astype(np.float32) ** 2

    images = Images(images.reshape(scan.gpts), sampling=scan.sampling)
    return images