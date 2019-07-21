"""
Equivalent Layer interpolators for harmonic functions
"""
import numpy as np
from numba import jit
from sklearn.utils.validation import check_is_fitted
from verde import get_region
from verde.base import BaseGridder, check_fit_input, least_squares

# Would use n_1d_arrays from verde.base when a new release is made


class HarmonicEQL(BaseGridder):
    r"""
    3D Equivalent Layer interpolator for harmonic fields using Green's functions

    This gridder assumes Cartesian coordinates.

    Predict values of an harmonic function. It uses point sources to build the
    Equivalent Layer, fitting the coefficients that correspond to each point source in
    order to fit the data values. Uses as Green's functions the inverse distance between
    the grid coordinates and the point source:

    .. math::

        \phi_m(P, Q) = \frac{1}{|P - Q|}

    where :math:`P` and :math:`Q` are the coordinates of the observation point and the
    source, respectively.

    Parameters
    ----------
    damping : None or float
        The positive damping regularization parameter. Controls how much smoothness is
        imposed on the estimated coefficients. If None, no regularization is used.
    points : None or list of arrays (optional)
        List containing the coordinates of the point sources used as Equivalent Layer
        in the following order: (``easting``, ``northing``, ``vertical``). If None,
        a default set of points will be created putting a single point source bellow
        each observation point at a depth proportional to  the distance to the nearest
        observation point [Cooper2000]_. Default None.
    depth_factor : float (optional)
        Adimensional factor to set the depth of each point source.
        If ``points`` is None, a default set of point will be created putting
        a single point source bellow each obervation point at a depth given by the
        product of the ``depth_factor`` and the distance to the nearest obervation
        point. A greater ``depth_factor`` will increase the depth of the point
        source. This parameter is ignored if ``points`` is not None. Default 3.

    Attributes
    ----------
    points_ : 2d-array
        Coordinates of the point sources used to build the Equivalent Layer.
    coefs_ : array
        Estimated coefficients of every point source.
    region_ : tuple
        The boundaries (``[W, E, S, N]``) of the data used to fit the interpolator.
        Used as the default region for the :meth:`~harmonica.HarmonicEQL.grid` and
        :meth:`~harmonica.HarmonicEQL.scatter` methods.
    """

    def __init__(self, damping=None, points=None, depth_factor=3):
        self.damping = damping
        self.points = points
        self.depth_factor = depth_factor

    def fit(self, coordinates, data, weights=None):
        """
        Fit the coefficients of the Equivalent Layer.

        The data region is captured and used as default for the
        :meth:`~harmonica.HarmonicEQL.grid` and :meth:`~harmonica.HarmonicEQL.scatter`
        methods.

        All input arrays must have the same shape.

        Parameters
        ----------
        coordinates : tuple of arrays
            Arrays with the coordinates of each data point. Should be in the
            following order: (easting, northing, vertical, ...). Only easting
            and northing will be used, all subsequent coordinates will be
            ignored.
        data : array
            The data values of each data point.
        weights : None or array
            If not None, then the weights assigned to each data point.
            Typically, this should be 1 over the data uncertainty squared.

        Returns
        -------
        self
            Returns this estimator instance for chaining operations.
        """
        coordinates, data, weights = check_fit_input(coordinates, data, weights)
        # Capture the data region to use as a default when gridding.
        self.region_ = get_region(coordinates[:2])
        coordinates = tuple(np.atleast_1d(i).ravel() for i in coordinates[:3])
        if self.points is None:
            # Put a single point source bellow each observation point at a depth three
            # times the distance to the nearest observation point.
            nearest_distances = np.zeros(coordinates[0].size)
            distance_to_nearest_point(coordinates, nearest_distances)
            point_east, point_north, point_vertical = tuple(
                np.atleast_1d(i).ravel().copy() for i in coordinates[:3]
            )
            point_vertical -= self.depth_factor * nearest_distances
            self.points_ = (point_east, point_north, point_vertical)
        else:
            self.points_ = tuple(np.atleast_1d(i).ravel() for i in self.points[:3])
        jacobian = self.jacobian(coordinates, self.points_)
        self.coefs_ = least_squares(jacobian, data, weights, self.damping)
        return self

    def predict(self, coordinates):
        """
        Evaluate the estimated spline on the given set of points.

        Requires a fitted estimator (see :meth:`~harmonica.HarmonicEQL.fit`).

        Parameters
        ----------
        coordinates : tuple of arrays
            Arrays with the coordinates of each data point. Should be in the
            following order: (``easting``, ``northing``, ``vertical``, ...). Only
            ``easting``, ``northing`` and ``vertical`` will be used, all subsequent
            coordinates will be ignored.

        Returns
        -------
        data : array
            The data values evaluated on the given points.
        """
        # We know the gridder has been fitted if it has the coefs_
        check_is_fitted(self, ["coefs_"])
        shape = np.broadcast(*coordinates[:3]).shape
        size = np.broadcast(*coordinates[:3]).size
        dtype = coordinates[0].dtype
        coordinates = [np.atleast_1d(i).ravel() for i in coordinates[:3]]
        data = np.zeros(size, dtype=dtype)
        predict_numba(coordinates, self.points_, self.coefs_, data)
        return data.reshape(shape)

    def jacobian(self, coordinates, points, dtype="float64"):
        """
        Make the Jacobian matrix for the Equivalent Layer.

        Each column of the Jacobian is the Green's function for a single point source
        evaluated on all observation points [Sandwell1987]_.

        Parameters
        ----------
        coordinates : tuple of arrays
            Arrays with the coordinates of each data point. Should be in the
            following order: (``easting``, ``northing``, ``vertical``, ...). Only
            ``easting``, ``northing`` and ``vertical`` will be used, all subsequent
            coordinates will be ignored.
        points : tuple of arrays
            Tuple of arrays containing the coordinates of the point sources used as
            Equivalent Layer in the following order: (``easting``, ``northing``,
            ``vertical``).
        dtype : str or numpy dtype
            The type of the Jacobian array.

        Returns
        -------
        jacobian : 2D array
            The (n_data, n_points) Jacobian matrix.
        """
        n_data = coordinates[0].size
        n_points = points[0].size
        jac = np.zeros((n_data, n_points), dtype=dtype)
        jacobian_numba(coordinates, points, jac)
        return jac


@jit(nopython=True)
def predict_numba(coordinates, points, coeffs, result):
    """
    Calculate the predicted data using numba for speeding things up.
    """
    east, north, vertical = coordinates[:]
    point_east, point_north, point_vertical = points[:]
    for i in range(east.size):
        for j in range(point_east.size):
            result[i] += coeffs[j] * greens_func(
                east[i],
                north[i],
                vertical[i],
                point_east[j],
                point_north[j],
                point_vertical[j],
            )


@jit(nopython=True)
def greens_func(east, north, vertical, point_east, point_north, point_vertical):
    """
    Calculate the Green's function for the Equivalent Layer using numba.
    """
    return 1 / distance(east, north, vertical, point_east, point_north, point_vertical)


@jit(nopython=True)
def jacobian_numba(coordinates, points, jac):
    """
    Calculate the Jacobian matrix using numba to speed things up.
    """
    east, north, vertical = coordinates[:]
    point_east, point_north, point_vertical = points[:]
    for i in range(east.size):
        for j in range(point_east.size):
            jac[i, j] = greens_func(
                east[i],
                north[i],
                vertical[i],
                point_east[j],
                point_north[j],
                point_vertical[j],
            )


@jit(nopython=True)
def distance(east_1, north_1, vertical_1, east_2, north_2, vertical_2):
    """
    Compute the distance between two points
    """
    dist = np.sqrt(
        (east_1 - east_2) ** 2
        + (north_1 - north_2) ** 2
        + (vertical_1 - vertical_2) ** 2
    )
    return dist


@jit(nopython=True)
def distance_to_nearest_point(coordinates, distances):
    """
    Compute the distance to the nearest point for each observation point
    """
    east, north, vertical = coordinates[:]
    for i in range(east.size):
        # Initialize min_dist with the distance between the i-th and the (i-1)-th points
        distances[i] = distance(
            east[i], north[i], vertical[i], east[i - 1], north[i - 1], vertical[i - 1]
        )
        for j in range(east.size):
            if i != j:
                dist = distance(
                    east[i], north[i], vertical[i], east[j], north[j], vertical[j]
                )
                if dist < distances[i]:
                    distances[i] = dist