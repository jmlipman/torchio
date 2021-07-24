import random
import warnings
from itertools import islice
from typing import List, Iterator, Optional, Sequence

import humanize
from torch.utils.data import Dataset, DataLoader, RandomSampler

from .subject import Subject
from .sampler import PatchSampler
from .dataset import SubjectsDataset


class Queue(Dataset):
    r"""Queue used for stochastic patch-based training.

    A training iteration (i.e., forward and backward pass) performed on a
    GPU is usually faster than loading, preprocessing, augmenting, and cropping
    a volume on a CPU.
    Most preprocessing operations could be performed using a GPU,
    but these devices are typically reserved for training the CNN so that batch
    size and input tensor size can be as large as possible.
    Therefore, it is beneficial to prepare (i.e., load, preprocess and augment)
    the volumes using multiprocessing CPU techniques in parallel with the
    forward-backward passes of a training iteration.
    Once a volume is appropriately prepared, it is computationally beneficial to
    sample multiple patches from a volume rather than having to prepare the same
    volume each time a patch needs to be extracted.
    The sampled patches are then stored in a buffer or *queue* until
    the next training iteration, at which point they are loaded onto the GPU
    for inference.
    For this, TorchIO provides the :class:`~torchio.data.Queue` class, which also
    inherits from the PyTorch :class:`~torch.utils.data.Dataset`.
    In this queueing system,
    samplers behave as generators that yield patches from random locations
    in volumes contained in the :class:`~torchio.data.SubjectsDataset`.

    The end of a training epoch is defined as the moment after which patches
    from all subjects have been used for training.
    At the beginning of each training epoch,
    the subjects list in the :class:`~torchio.data.SubjectsDataset` is shuffled,
    as is typically done in machine learning pipelines to increase variance
    of training instances during model optimization.
    A PyTorch loader queries the datasets copied in each process,
    which load and process the volumes in parallel on the CPU.
    A patches list is filled with patches extracted by the sampler,
    and the queue is shuffled once it has reached a specified maximum length so
    that batches are composed of patches from different subjects.
    The internal data loader continues querying the
    :class:`~torchio.data.SubjectsDataset` using multiprocessing.
    The patches list, when emptied, is refilled with new patches.
    A second data loader, external to the queue,
    may be used to collate batches of patches stored in the queue,
    which are passed to the neural network.

    Args:
        subjects_dataset: Instance of :class:`~torchio.data.SubjectsDataset`.
        max_length: Maximum number of patches that can be stored in the queue.
            Using a large number means that the queue needs to be filled less
            often, but more CPU memory is needed to store the patches.
        samples_per_volume: List with the number of patches to extract from each
            volume. If a number is given, the same number of patches per volume
            is used. A small number of patches ensures a large variability in
            the queue, but training will be slower.
        sampler: A subclass of :class:`~torchio.data.sampler.PatchSampler` used
            to extract patches from the volumes.
        num_workers: Number of subprocesses to use for data loading
            (as in :class:`torch.utils.data.DataLoader`).
            ``0`` means that the data will be loaded in the main process.
        shuffle_subjects: If ``True``, the subjects dataset is shuffled at the
            beginning of each epoch, i.e. when all patches from all subjects
            have been processed.
        shuffle_patches: If ``True``, patches are shuffled after filling the
            queue.
        start_background: If ``True``, the loader will start working in the
            background as soon as the queue is instantiated.
        verbose: If ``True``, some debugging messages will be printed.

    This diagram represents the connection between
    a :class:`~torchio.data.SubjectsDataset`,
    a :class:`~torchio.data.Queue`
    and the :class:`~torch.utils.data.DataLoader` used to pop batches from the
    queue.

    .. image:: https://raw.githubusercontent.com/fepegar/torchio/master/docs/images/diagram_patches.svg
        :alt: Training with patches

    This sketch can be used to experiment and understand how the queue works.
    In this case, :attr:`shuffle_subjects` is ``False``
    and :attr:`shuffle_patches` is ``True``.

    .. raw:: html

        <embed>
            <iframe style="width: 640px; height: 360px; overflow: hidden;" scrolling="no" frameborder="0" src="https://editor.p5js.org/embed/DZwjZzkkV"></iframe>
        </embed>

    .. note:: :attr:`num_workers` refers to the number of workers used to
        load and transform the volumes. Multiprocessing is not needed to pop
        patches from the queue, so you should always use ``num_workers=0`` for
        the :class:`~torch.utils.data.DataLoader` you instantiate to generate
        training batches.

    Example:

    >>> import torch
    >>> import torchio as tio
    >>> from torch.utils.data import DataLoader
    >>> patch_size = 96
    >>> queue_length = 300
    >>> samples_per_volume = 10
    >>> sampler = tio.data.UniformSampler(patch_size)
    >>> subject = tio.datasets.Colin27()
    >>> subjects_dataset = tio.SubjectsDataset(10 * [subject])
    >>> patches_queue = tio.Queue(
    ...     subjects_dataset,
    ...     queue_length,
    ...     samples_per_volume,
    ...     sampler,
    ...     num_workers=4,
    ... )
    >>> patches_loader = DataLoader(patches_queue, batch_size=16)
    >>> num_epochs = 2
    >>> model = torch.nn.Identity()
    >>> for epoch_index in range(num_epochs):
    ...     for patches_batch in patches_loader:
    ...         inputs = patches_batch['t1'][tio.DATA]  # key 't1' is in subject
    ...         targets = patches_batch['brain'][tio.DATA]  # key 'brain' is in subject
    ...         logits = model(inputs)  # model being an instance of torch.nn.Module

    """  # noqa: E501
    def __init__(
            self,
            subjects_dataset: SubjectsDataset,
            max_length: int,
            samples_per_volume: int,
            sampler: PatchSampler,
            num_workers: int = 0,
            shuffle_subjects: bool = True,
            shuffle_patches: bool = True,
            start_background: bool = True,
            verbose: bool = False,
            ):
        self.subjects_dataset = subjects_dataset
        self.max_length = max_length
        self.shuffle_subjects = shuffle_subjects
        self.shuffle_patches = shuffle_patches
        self.samples_per_volume = samples_per_volume
        if isinstance(samples_per_volume, int):
            self.samples_per_volume = self.num_subjects * [samples_per_volume]

        if not isinstance(self.samples_per_volume, Sequence):
            raise TypeError('`samples_per_volume` should be an int or a list')

        if len(self.samples_per_volume) != self.num_subjects:
            raise ValueError('`samples_per_volume` (list) length must be equal'
                             'to the number of subjects')

        self.sampler = sampler
        self.num_workers = num_workers
        self.verbose = verbose
        self._subjects_iterable = None
        self.patches_list: List[Subject] = []
        self.num_sampled_patches = 0

        if start_background:
            self._initialize_subjects_iterable()

        # Keeps a list of the remaining patches to be extracted
        self.counter_samples_per_volume = self.samples_per_volume.copy()
        # Helps keeping track of which subject it needs to extract patches
        self.idx_subject = -1
        # Subject. Save as an object property to save computations later
        # (more details in _fill())
        self.curr_subject = None

    def __len__(self):
        return self.iterations_per_epoch

    def __getitem__(self, _):
        # There are probably more elegant ways of doing this
        if not self.patches_list:
            self._print('Patches list is empty.')
            self._fill()
        sample_patch = self.patches_list.pop()
        self.num_sampled_patches += 1
        return sample_patch

    def __repr__(self):
        attributes = [
            f'max_length={self.max_length}',
            f'num_subjects={self.num_subjects}',
            f'num_patches={self.num_patches}',
            f'samples_per_volume={self.samples_per_volume}',
            f'num_sampled_patches={self.num_sampled_patches}',
            f'iterations_per_epoch={self.iterations_per_epoch}',
        ]
        attributes_string = ', '.join(attributes)
        return f'Queue({attributes_string})'

    def _print(self, *args):
        if self.verbose:
            print(*args)  # noqa: T001

    def _initialize_subjects_iterable(self):
        self._subjects_iterable = self._get_subjects_iterable()

    @property
    def subjects_iterable(self):
        if self._subjects_iterable is None:
            self._initialize_subjects_iterable()
        return self._subjects_iterable

    @property
    def num_subjects(self) -> int:
        return len(self.subjects_dataset)

    @property
    def num_patches(self) -> int:
        return len(self.patches_list)

    @property
    def iterations_per_epoch(self) -> int:
        return sum(self.samples_per_volume)

    def _fill(self) -> None:
        assert self.sampler is not None
        if self.max_length % self.iterations_per_epoch != 0:
            message = (
                f'Queue length ({self.max_length})'
                ' not divisible by the number of'
                f' patches per volume ({self.samples_per_volume})'
            )
            warnings.warn(message, RuntimeWarning)

        # If the counter of samples per volume is empty (i.e., end of the
        # epoch), refill it.
        if sum(self.counter_samples_per_volume) == 0:
            self._initialize_subjects_iterable()
            self.counter_samples_per_volume = self.samples_per_volume.copy()
            self.idx_subject = -1
            self.curr_subject = None

        # Add patches
        # 3 stopping conditions (OR):
        #   1) The number of current patches in patches_list >= max patches
        #   2) There are no more patches that need to be added
        #      (i.e., remaining patches -> 0)
        #   3) There are no more subjects to extract patches.
        while (len(self.patches_list) < self.max_length
                and sum(self.counter_samples_per_volume) != 0
                and self.idx_subject < self.num_subjects):

            if (self.curr_subject is None
                    or self.counter_samples_per_volume[self.idx_subject] == 0):

                self.curr_subject = self._get_next_subject()
                self.idx_subject += 1

            # Whether to fill the Queue with a "portion" of patches
            # of a specific subject, or all patches of that subject.
            if (len(self.patches_list)
                    + self.counter_samples_per_volume[self.idx_subject]
                    > self.max_length):
                # Take a portion
                spv = self.max_length - len(self.patches_list)
            else:
                spv = self.counter_samples_per_volume[self.idx_subject]

            self.counter_samples_per_volume[self.idx_subject] -= spv
            iterable = self.sampler(self.curr_subject)
            patches = list(islice(iterable, spv))
            self.patches_list.extend(patches)

        if self.shuffle_patches:
            random.shuffle(self.patches_list)
        else:
            # Reverse the order of the patches so that list().pop starts
            # from the beginning
            self.patches_list = self.patches_list[::-1]

    def _get_next_subject(self) -> Subject:
        # A StopIteration exception is expected when the queue is empty
        try:
            subject = next(self.subjects_iterable)
        except StopIteration as exception:
            self._print('Queue is empty:', exception)
            self._initialize_subjects_iterable()
            subject = next(self.subjects_iterable)
        return subject

    @staticmethod
    def _get_first_item(batch):
        return batch[0]

    def _get_subjects_iterable(self) -> Iterator:
        # I need a DataLoader to handle parallelism
        # But this loader is always expected to yield single subject samples

        # Same random shuffling applied to subjects and volumes
        if self.shuffle_subjects:
            random_idx = list(RandomSampler(self.subjects_dataset))
            local_sub_dataset = [self.subjects_dataset[i] for i in random_idx]
            self.samples_per_volume = [self.samples_per_volume[i]
                                       for i in random_idx]
        else:
            local_sub_dataset = self.subjects_dataset

        self._print(
            f'\nCreating subjects loader with {self.num_workers} workers')
        subjects_loader = DataLoader(
            local_sub_dataset,
            num_workers=self.num_workers,
            batch_size=1,
            collate_fn=self._get_first_item,
            shuffle=False,  # Shuffling is done is Queue's constructor
        )
        return iter(subjects_loader)

    def get_max_memory(self, subject: Optional[Subject] = None) -> int:
        """Get the maximum RAM occupied by the patches queue in bytes.

        Args:
            subject: Sample subject to compute the size of a patch.
        """
        images_channels = 0
        if subject is None:
            subject = self.subjects_dataset[0]
        for image in subject.get_images(intensity_only=False):
            images_channels += len(image.data)
        voxels_in_patch = int(self.sampler.patch_size.prod() * images_channels)
        bytes_per_patch = 4 * voxels_in_patch  # assume float32
        return int(bytes_per_patch * self.max_length)

    def get_max_memory_pretty(self, subject: Optional[Subject] = None) -> str:
        """Get human-readable maximum RAM occupied by the patches queue.

        Args:
            subject: Sample subject to compute the size of a patch.
        """
        memory = self.get_max_memory(subject=subject)
        return humanize.naturalsize(memory, binary=True)
