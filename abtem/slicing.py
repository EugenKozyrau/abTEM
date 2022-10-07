from abc import abstractmethod
from typing import Tuple, Union, Sequence

import numpy as np
from ase import Atoms

from abtem.core.utils import label_to_index
from abtem.atoms import is_cell_orthogonal


def validate_slice_thickness(slice_thickness: Union[float, Tuple[float, ...]],
                             thickness: float = None,
                             num_slices: int = None) -> Tuple[float, ...]:
    if np.isscalar(slice_thickness):
        if thickness is not None:
            n = np.ceil(thickness / slice_thickness)
            slice_thickness = (thickness / n,) * int(n)
        elif num_slices is not None:
            slice_thickness = (slice_thickness,) * num_slices
        else:
            raise RuntimeError()

    slice_thickness = tuple(slice_thickness)

    if thickness is not None:
        if not np.isclose(np.sum(slice_thickness), thickness):
            raise RuntimeError()

    if num_slices is not None:
        if len(slice_thickness) != num_slices:
            raise RuntimeError()

    return slice_thickness


def _slice_limits(slice_thickness):
    cumulative_thickness = np.cumsum(np.concatenate(((0,), slice_thickness)))
    return [(cumulative_thickness[i], cumulative_thickness[i + 1]) for i in range(len(cumulative_thickness) - 1)]


def unpack_item(item, num_items):
    if isinstance(item, int):
        first_index = item
        last_index = first_index + 1
    elif isinstance(item, slice):
        first_index = 0 if item.start is None else item.start
        last_index = num_items if item.stop is None else item.stop
    else:
        raise RuntimeError()

    if last_index is None:
        last_index = num_items
    else:
        last_index = min(last_index, num_items)

    if first_index >= last_index:
        raise IndexError

    return first_index, last_index


class AbstractSlicedAtoms:

    def __init__(self, atoms: Atoms, slice_thickness: Union[float, np.ndarray, str]):

        if not is_cell_orthogonal(atoms):
            raise RuntimeError('atoms must have an orthgonal cell')

        self._atoms = atoms

        if isinstance(slice_thickness, str):
            raise NotImplementedError

        self._slice_thickness = validate_slice_thickness(slice_thickness, thickness=atoms.cell[2, 2])

    def __len__(self):
        return self.num_slices

    @property
    def atoms(self):
        return self._atoms

    @property
    def box(self) -> Tuple[float, float, float]:
        return tuple(np.diag(self._atoms.cell))

    @property
    def num_slices(self) -> int:
        return len(self._slice_thickness)

    @property
    def slice_thickness(self):
        return self._slice_thickness

    @property
    def slice_limits(self):
        return _slice_limits(self.slice_thickness)

    def check_slice_idx(self, i):
        """Raises an error if i is greater than the number of slices."""
        if i >= self.num_slices:
            raise RuntimeError('Slice index {} too large for sliced atoms with {} slices'.format(i, self.num_slices))

    @abstractmethod
    def get_atoms_in_slices(self, first_slice: int, last_slice: int, **kwargs):
        pass

    def __getitem__(self, item):
        return self.get_atoms_in_slices(*unpack_item(item, len(self)))


class SliceIndexedAtoms(AbstractSlicedAtoms):

    def __init__(self,
                 atoms: Atoms,
                 slice_thickness: Union[float, Tuple[float, ...]]):

        super().__init__(atoms, slice_thickness)

        labels = np.digitize(self.atoms.positions[:, 2], np.cumsum(self.slice_thickness))
        self._slice_index = [indices for indices in label_to_index(labels, max_label=len(self) - 1)]

    @property
    def slice_index(self):
        return self._slice_index

    def get_atoms_in_slices(self, first_slice: int, last_slice: int = None, atomic_number: int = None):
        if last_slice is None:
            last_slice = first_slice

        if last_slice - first_slice < 2:
            in_slice = self.slice_index[first_slice]
        else:
            in_slice = np.concatenate(self.slice_index[first_slice:last_slice])

        atoms = self.atoms[in_slice]

        if atomic_number is not None:
            atoms = atoms[(atoms.numbers == atomic_number)]

        slice_thickness = self.slice_thickness[first_slice:last_slice]
        atoms.cell[2, 2] = np.sum(slice_thickness)
        atoms.positions[:, 2] -= np.sum(self.slice_thickness[:first_slice])
        return atoms


class SlicedAtoms(AbstractSlicedAtoms):

    def __init__(self,
                 atoms: Atoms,
                 slice_thickness: Union[float, Sequence[float]],
                 xy_padding: float = 0.,
                 z_padding: float = 0.):

        super().__init__(atoms, slice_thickness)
        self._xy_padding = xy_padding
        self._z_padding = z_padding

    def get_atoms_in_slices(self,
                            first_slice: int,
                            last_slice: int = None,
                            atomic_number: int = None):

        if last_slice is None:
            last_slice = first_slice

        a = self.slice_limits[first_slice][0]
        b = self.slice_limits[last_slice][0]

        in_slice = (self.atoms.positions[:, 2] >= (a - self._z_padding)) * \
                   (self.atoms.positions[:, 2] < (b + self._z_padding))

        if atomic_number is not None:
            in_slice = (self.atoms.numbers == atomic_number) * in_slice

        atoms = self.atoms[in_slice]
        atoms.cell = tuple(np.diag(atoms.cell)[:2]) + (b - a,)
        return atoms