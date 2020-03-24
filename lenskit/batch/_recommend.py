import os
import os.path
import pathlib
import tempfile
import logging
import warnings
from collections import namedtuple

from joblib import Parallel, delayed, dump, load

import pandas as pd
import numpy as np

from ..algorithms import Recommender
from .. import util
from ..sharing import sharing_mode

_logger = logging.getLogger(__name__)
_AlgoKey = namedtuple('AlgoKey', ['type', 'data'])


@util.last_memo(check_type='equality')
def __load_algo(path):
    return load(path, mmap_mode='r')


def _recommend_user(algo, user, n, candidates):
    if type(algo).__name__ == 'AlgoKey':  # pickling doesn't preserve isinstance
        if algo.type == 'file':
            algo = __load_algo(algo.data)
        else:
            raise ValueError('unknown algorithm key type %s', algo.type)

    _logger.debug('generating recommendations for %s', user)
    watch = util.Stopwatch()
    res = algo.recommend(user, n, candidates)
    _logger.debug('%s recommended %d/%s items for %s in %s', algo, len(res), n, user, watch)
    res['user'] = user
    res['rank'] = np.arange(1, len(res) + 1)
    return res


def __standard_cand_fun(candidates):
    """
    Convert candidates from the forms accepted by :py:fun:`recommend` into
    a standard form, a function that takes a user and returns a candidate
    list.
    """
    if isinstance(candidates, dict):
        return candidates.get
    elif candidates is None:
        return lambda u: None
    else:
        return candidates


def recommend(algo, users, n, candidates=None, *, n_jobs=None, **kwargs):
    """
    Batch-recommend for multiple users.  The provided algorithm should be a
    :py:class:`algorithms.Recommender`.

    Args:
        algo: the algorithm
        users(array-like): the users to recommend for
        n(int): the number of recommendations to generate (None for unlimited)
        candidates:
            the users' candidate sets. This can be a function, in which case it will
            be passed each user ID; it can also be a dictionary, in which case user
            IDs will be looked up in it.  Pass ``None`` to use the recommender's
            built-in candidate selector (usually recommended).
        n_jobs(int):
            The number of processes to use for parallel recommendations.  Passed as
            ``n_jobs`` to :cls:`joblib.Parallel`.  The default, ``None``, will result
            in a call to :func:`util.proc_count`(``None``), so the process will be
            the process sequential _unless_ called inside the :func:`joblib.parallel_backend`
            context manager or the ``LK_NUM_PROCS`` environment variable is set.

            .. note:: ``nprocs`` is accepted as a deprecated alias.

    Returns:
        A frame with at least the columns ``user``, ``rank``, and ``item``; possibly also
        ``score``, and any other columns returned by the recommender.
    """

    if n_jobs is None and 'nprocs' in kwargs:
        n_jobs = kwargs['nprocs']
        warnings.warn('nprocs is deprecated, use n_jobs', DeprecationWarning)

    if n_jobs is None:
        n_jobs = util.proc_count(None)

    rec_algo = Recommender.adapt(algo)
    if candidates is None and rec_algo is not algo:
        warnings.warn('no candidates provided and algo is not a recommender, unlikely to work')
    del algo  # don't need reference any more

    if 'ratings' in kwargs:
        warnings.warn('Providing ratings to recommend is not supported', DeprecationWarning)

    candidates = __standard_cand_fun(candidates)

    loop = Parallel(n_jobs=n_jobs)

    path = None
    try:
        _logger.debug('activating recommender loop')
        with loop:
            backend = loop._backend.__class__.__name__
            njobs = loop._effective_n_jobs()
            _logger.info('parallel backend %s, effective njobs %s',
                         backend, njobs)
            astr = str(rec_algo)
            if njobs > 1:
                fd, path = tempfile.mkstemp(prefix='lkpy-predict', suffix='.pkl',
                                            dir=util.scratch_dir(joblib=True))
                path = pathlib.Path(path)
                os.close(fd)
                _logger.debug('pre-serializing algorithm %s to %s', rec_algo, path)
                with sharing_mode():
                    dump(rec_algo, path)
                rec_algo = _AlgoKey('file', path)

            _logger.info('recommending with %s for %d users (n_jobs=%s)', astr, len(users), n_jobs)
            timer = util.Stopwatch()
            results = loop(delayed(_recommend_user)(rec_algo, user, n, candidates(user))
                           for user in users)

            results = pd.concat(results, ignore_index=True, copy=False)
            _logger.info('recommended for %d users in %s', len(users), timer)
    finally:
        util.delete_sometime(path)

    return results
