import numpy as np
from numba import njit


@njit(cache=True)
def states(idir, ng, dx, dt,
           irho, iu, iv, ip, ibx, iby, ix, nspec,
           gamma, qv, Bx, By):
    r"""
    predict the cell-centered state to the edges in one-dimension
    using the reconstructed, limited slopes.

    We follow the convection here that ``V_l[i]`` is the left state at the
    i-1/2 interface and ``V_l[i+1]`` is the left state at the i+1/2
    interface.

    We need the left and right eigenvectors and the eigenvalues for
    the system projected along the x-direction.

    Taking our state vector as :math:`Q = (\rho, u, v, p, bx, by)^T`, the eigenvalues
    are :math:`u - c`, :math:`u`, :math:`u + c`.

    We look at the equations of hydrodynamics in a split fashion --
    i.e., we only consider one dimension at a time.

    Considering advection in the x-direction, the Jacobian matrix for
    the primitive variable formulation of the Euler equations
    projected in the x-direction is::

             / u   r   0   0 \
             | 0   u   0  1/r |
         A = | 0   0   u   0  |
             \ 0  rc^2 0   u  /

    The right eigenvectors are::

             /  1  \        / 1 \        / 0 \        /  1  \
             |-c/r |        | 0 |        | 0 |        | c/r |
        r1 = |  0  |   r2 = | 0 |   r3 = | 1 |   r4 = |  0  |
             \ c^2 /        \ 0 /        \ 0 /        \ c^2 /

    In particular, we see from r3 that the transverse velocity (v in
    this case) is simply advected at a speed u in the x-direction.

    The left eigenvectors are::

         l1 =     ( 0,  -r/(2c),  0, 1/(2c^2) )
         l2 =     ( 1,     0,     0,  -1/c^2  )
         l3 =     ( 0,     0,     1,     0    )
         l4 =     ( 0,   r/(2c),  0, 1/(2c^2) )

    The fluxes are going to be defined on the left edge of the
    computational zones::

            |             |             |             |
            |             |             |             |
           -+------+------+------+------+------+------+--
            |     i-1     |      i      |     i+1     |
                         ^ ^           ^
                     q_l,i q_r,i  q_l,i+1

    q_r,i and q_l,i+1 are computed using the information in zone i,j.

    Parameters
    ----------
    idir : int
        Are we predicting to the edges in the x-direction (1) or y-direction (2)?
    ng : int
        The number of ghost cells
    dx : float
        The cell spacing
    dt : float
        The timestep
    irho, iu, iv, ip, ix : int
        Indices of the density, x-velocity, y-velocity, pressure and species in the
        state vector
    naux : int
        The number of species
    gamma : float
        Adiabatic index
    qv : ndarray
        The primitive state vector

    Returns
    -------
    out : ndarray, ndarray
        State vector predicted to the left and right edges
    """

    qx, qy, nvar = qv.shape

    q_l = np.zeros_like(qv)
    q_r = np.zeros_like(qv)

    nx = qx - 2 * ng
    ny = qy - 2 * ng
    ilo = ng
    ihi = ng + nx
    jlo = ng
    jhi = ng + ny

    # Going to use the simpler method from Stone & Gardiner 09, section 4.2 here
    dq_l = np.zeros_like(qv)
    dq_r = np.zeros_like(qv)
    dq_c = np.zeros_like(qv)

    # compute left-, right- and centered-differences of primitive variables
    if idir == 1:
        dq_l[1:-1, :, :] = qv[1:-1, :, :] - qv[:-2, :, :]
        dq_r[1:-1, :, :] = qv[2:, :, :] - qv[1:-1, :, :]
        dq_c[1:-1, :, :] = 0.5 * (qv[2:, :, :] - qv[:-2, :, :])
    else:
        dq_l[:, 1:-1, :] = qv[:, 1:-1, :] - qv[:, :-2, :]
        dq_r[:, 1:-1, :] = qv[:, 2:, :] - qv[:, 1:-1, :]
        dq_c[:, 1:-1, :] = 0.5 * (qv[:, 2:, :] - qv[:, :-2, :])

    # apply monotonicity constraints

    dq = np.sign(dq_c) * np.minimum(2. * np.abs(dq_l),
                                    np.minimum(2. * np.abs(dq_r), np.abs(dq_c)))

    # set longitudinal component of the magnetic field to be 0
    if idir == 1:
        dq[:, :, ibx] = 0
    else:
        dq[:, :, iby] = 0

    # compute left- and right-interface states using the monotonized difference in the primitive variables
    for i in range(ilo - 3, ihi + 3):
        for j in range(jlo - 3, jhi + 3):
            if idir == 1:
                q_l[i + 1, j, :] = qv[i, j] + 0.5 * dq[i, j]
                q_r[i, j] = qv[i, j] - 0.5 * dq[i, j]
            else:
                q_l[i, j + 1] = qv[i, j] + 0.5 * dq[i, j]
                q_r[i, j] = qv[i, j] - 0.5 * dq[i, j]

    return q_l, q_r


@njit(cache=True)
def riemann_adiabatic(idir, ng,
                      idens, ixmom, iymom, iener, ixmag, iymag, irhoX, nspec,
                      lower_solid, upper_solid,
                      gamma, U_l, U_r, Bx, By):
    r"""
    HLLE solver for adiabatic magnetohydrodynamics.
    """

    qx, qy, nvar = U_l.shape

    F = np.zeros_like(U_l)

    smallc = 1.e-10
    # smallrho = 1.e-10
    smallp = 1.e-10

    nx = qx - 2 * ng
    ny = qy - 2 * ng
    ilo = ng
    ihi = ng + nx
    jlo = ng
    jhi = ng + ny

    for i in range(ilo - 2, ihi + 2):
        for j in range(jlo - 2, jhi + 2):
            # primitive variable states
            rho_l = U_l[i, j, idens]

            # un = normal velocity; ut = transverse velocity
            if (idir == 1):
                un_l = U_l[i, j, ixmom] / rho_l
                ut_l = U_l[i, j, iymom] / rho_l
            else:
                un_l = U_l[i, j, iymom] / rho_l
                ut_l = U_l[i, j, ixmom] / rho_l

            if (idir == 1):
                # if we're looking at flux in x-direction, can use x-face centered
                # Bx, but need to get By from U as it's y-face centered
                Bx_l = Bx[i, j]
                Bx_r = Bx[i, j]

                By_l = U_l[i, j, iymag]
                By_r = U_r[i, j, iymag]
            else:
                # the reverse is true for flux in the y-direction
                By_l = By[i, j]
                By_r = By[i, j]
                Bx_l = U_l[i, j, ixmag]
                Bx_r = U_r[i, j, ixmag]

            B2_l = Bx_l**2 + By_l**2
            B2_r = Bx_r**2 + By_r**2

            rhoe_l = U_l[i, j, iener] - 0.5 * rho_l * (un_l**2 + ut_l**2) - \
                0.5 * B2_l

            p_l = rhoe_l * (gamma - 1.0)
            p_l = max(p_l, smallp)

            rho_r = U_r[i, j, idens]

            if (idir == 1):
                un_r = U_r[i, j, ixmom] / rho_r
                ut_r = U_r[i, j, iymom] / rho_r
            else:
                un_r = U_r[i, j, iymom] / rho_r
                ut_r = U_r[i, j, ixmom] / rho_r

            rhoe_r = U_r[i, j, iener] - 0.5 * rho_r * (un_r**2 + ut_r**2) - \
                0.5 * B2_r

            p_r = rhoe_r * (gamma - 1.0)
            p_r = max(p_r, smallp)

            # and the regular sound speeds
            c_l = max(smallc, np.sqrt(gamma * p_l / rho_l))
            c_r = max(smallc, np.sqrt(gamma * p_r / rho_r))

            bx_l = Bx_l / np.sqrt(4 * np.pi)
            bx_r = Bx_r / np.sqrt(4 * np.pi)
            by_l = By_l / np.sqrt(4 * np.pi)
            by_r = By_r / np.sqrt(4 * np.pi)

            # find the Roe average stuff
            # we have to annoyingly do this for the primitive variables then convert back.

            # U_av = (U_l[i, j, :] * np.sqrt(rho_l) + U_r[i, j, :] * np.sqrt(rho_r)) / \
            #     (np.sqrt(rho_l) + np.sqrt(rho_r))

            q_av = np.zeros_like(U_l[i, j, :])
            q_av[idens] = np.sqrt(rho_l * rho_r)
            # these are actually the primitive velocities
            q_av[ixmom] = (U_l[i, j, ixmom] / np.sqrt(rho_l) + U_r[i, j, ixmom] / np.sqrt(rho_r)) / \
                (np.sqrt(rho_l) + np.sqrt(rho_r))
            q_av[iymom] = (U_l[i, j, iymom] / np.sqrt(rho_l) + U_r[i, j, iymom] / np.sqrt(rho_r)) / \
                (np.sqrt(rho_l) + np.sqrt(rho_r))

            # this is the enthalpy

            h_l = (gamma * U_l[i, j, iener] -
                   (gamma - 1.0) * (U_l[i, j, ixmom]**2 + U_l[i, j, iymom]**2) / rho_l +
                   0.5 * gamma * B2_l) / rho_l

            h_r = (gamma * U_r[i, j, iener] -
                   (gamma - 1.0) * (U_r[i, j, ixmom]**2 + U_r[i, j, iymom]**2) / rho_r +
                   0.5 * gamma * B2_r) / rho_r

            q_av[iener] = (h_l * np.sqrt(rho_l) + h_r * np.sqrt(rho_r)) / \
                (np.sqrt(rho_l) + np.sqrt(rho_r))

            q_av[ixmag:iymag + 1] = (U_l[i, j, ixmag:iymag + 1] * np.sqrt(rho_l) +
                                     U_r[i, j, ixmag:iymag + 1] * np.sqrt(rho_r)) / (np.sqrt(rho_l) + np.sqrt(rho_r))

            q_av[irhoX:] = (U_l[i, j, irhoX:] / np.sqrt(rho_l) +
                            U_r[i, j, irhoX:] / np.sqrt(rho_r)) / (np.sqrt(rho_l) + np.sqrt(rho_r))

            U_av = np.zeros_like(q_av)

            U_av[idens] = q_av[idens]
            U_av[ixmom] = q_av[idens] * q_av[ixmom]
            U_av[iymom] = q_av[idens] * q_av[iymom]
            U_av[iener] = (q_av[iener] * q_av[idens] +
                           (gamma - 1) * q_av[idens] * (q_av[ixmom]**2 + q_av[iymom]**2) -
                           0.5 * gamma * (q_av[ixmag]**2 + q_av[iymag]**2)) / gamma
            U_av[ixmag:iymag + 1] = q_av[ixmag:iymag + 1]
            U_av[irhoX:] = q_av[idens] * q_av[irhoX:]

            if idir == 1:
                X = 0.5 * (by_l - by_r)**2 / (np.sqrt(rho_l) + np.sqrt(rho_r))
            else:
                X = 0.5 * (bx_l - bx_r)**2 / (np.sqrt(rho_l) + np.sqrt(rho_r))

            Y = 0.5 * (rho_l + rho_r) / U_av[idens]

            evals = calc_evals(idir, U_av, gamma, idens, ixmom, iymom, iener,
                               ixmag, iymag, irhoX, X, Y)

            # now need to repeat all that stuff to find fast magnetosonic speed
            # in left and right states
            cA2 = (bx_l**2 + by_l**2) / rho_l

            if idir == 1:
                cAx2 = bx_l**2 / rho_l
            else:
                cAx2 = by_l**2 / rho_l

            cf_l = np.sqrt(
                0.5 * (c_l**2 + cA2 + np.sqrt((c_l**2 + cA2)**2 - 4 * c_l**2 * cAx2)))

            cA2 = (bx_r**2 + by_r**2) / rho_r

            if idir == 1:
                cAx2 = bx_r**2 / rho_r
            else:
                cAx2 = by_r**2 / rho_r

            cf_r = np.sqrt(
                0.5 * (c_r**2 + cA2 + np.sqrt((c_r**2 + cA2)**2 - 4 * c_r**2 * cAx2)))

            bp = max(max(np.max(evals), un_r + cf_r), 0)
            bm = min(min(np.min(evals), un_l - cf_l), 0)

            f_l = consFlux(idir, gamma, idens, ixmom, iymom, iener,
                           ixmag, iymag, irhoX, nspec, U_l[i, j, :])
            f_r = consFlux(idir, gamma, idens, ixmom, iymom, iener,
                           ixmag, iymag, irhoX, nspec, U_r[i, j, :])

            F[i, j, :] = (bp * f_l - bm * f_r) / (bp - bm) + \
                bp * bm / (bp - bm) * (U_r[i, j, :] - U_l[i, j, :])

    return F


@njit(cache=True)
def calc_evals(idir, U, gamma, idens, ixmom, iymom, iener, ixmag, iymag, irhoX, X, Y):
    r"""
    Calculate the eigenvalues using section B.3 in Stone, Gardiner et. al 08
    """
    dens = U[idens]
    u = U[ixmom] / U[idens]
    v = U[iymom] / U[idens]
    E = U[iener]
    Bx = U[ixmag]
    By = U[iymag]
    bx = Bx / np.sqrt(4 * np.pi)
    by = By / np.sqrt(4 * np.pi)
    B2 = Bx**2 + By**2
    b2 = bx**2 + by**2
    rhoe = E - 0.5 * dens * (u**2 + v**2) - 0.5 * b2
    P = rhoe * (gamma - 1.0)
    H = (E + P + 0.5 * b2) / dens

    gamma_d = gamma - 1.0
    X_d = (gamma - 2.) * X
    Y_d = (gamma - 2.) * Y

    a2 = gamma_d * (H - 0.5 * (u**2 + v**2) - b2 / dens) - X_d

    if idir == 1:
        CAx2 = bx**2 / dens
        b_norm2 = (gamma_d - Y_d) * by**2
    else:
        CAx2 = by**2 / dens
        b_norm2 = (gamma_d - Y_d) * bx**2

    CA2 = CAx2 + b_norm2 / dens

    Cf2 = 0.5 * ((a2 + CA2) + np.sqrt((a2 + CA2)**2 - 4 * a2 * CAx2))
    Cs2 = 0.5 * ((a2 + CA2) - np.sqrt((a2 + CA2)**2 - 4 * a2 * CAx2))

    if idir == 1:
        vx = u
    else:
        vx = v

    evals = np.array([vx - np.sqrt(Cf2), vx - np.sqrt(CAx2), vx - np.sqrt(Cs2),
                      vx, vx + np.sqrt(Cs2), vx + np.sqrt(CAx2), vx + np.sqrt(Cf2)])

    return evals


@njit(cache=True)
def consFlux(idir, gamma, idens, ixmom, iymom, iener, ixmag, iymag, irhoX, naux, U_state):
    r"""
    Calculate the conservative flux.

    Parameters
    ----------
    idir : int
        Are we predicting to the edges in the x-direction (1) or y-direction (2)?
    gamma : float
        Adiabatic index
    idens, ixmom, iymom, iener, ixmag, iymag, irhoX : int
        The indices of the density, x-momentum, y-momentum, x-magnetic field,
        y-magnetic field, internal energy density
        and species partial densities in the conserved state vector.
    naux : int
        The number of species
    U_state : ndarray
        Conserved state vector.

    Returns
    -------
    out : ndarray
        Conserved flux
    """

    F = np.zeros_like(U_state)

    u = U_state[ixmom] / U_state[idens]
    v = U_state[iymom] / U_state[idens]
    bx = U_state[ixmag]
    by = U_state[iymag]
    b2 = bx**2 + by**2

    p = (U_state[iener] - 0.5 * U_state[idens] *
         (u**2 + v**2) - 0.5 * b2) * (gamma - 1.0)

    if (idir == 1):
        F[idens] = U_state[idens] * u
        F[ixmom] = U_state[ixmom] * u + p + 0.5 * b2 - bx**2
        F[iymom] = U_state[iymom] * u - bx * by
        F[iener] = (U_state[iener] + p + 0.5 * b2) * u - \
            bx * (bx * u + by * v)
        F[ixmag] = 0
        F[iymag] = by * u - bx * v

        if (naux > 0):
            F[irhoX:irhoX + naux] = U_state[irhoX:irhoX + naux] * u

    else:
        F[idens] = U_state[idens] * v
        F[ixmom] = U_state[ixmom] * v - bx * by
        F[iymom] = U_state[iymom] * v + p + 0.5 * b2 - by**2
        F[iener] = (U_state[iener] + p + 0.5 * b2) * v - \
            by * (bx * u + by * v)
        F[ixmag] = bx * v - by * u
        F[iymag] = 0

        if (naux > 0):
            F[irhoX:irhoX + naux] = U_state[irhoX:irhoX + naux] * v

    return F


@njit(cache=True)
def emf(ng, idens, ixmom, iymom, iener, ixmag, iymag, irhoX, dx, dy, U, Fx, Fy,
        Eref=np.zeros((1, 1)), use_ref=False):
    r"""
    Calculate the EMF at cell corners.

    Eref is the cell-centered reference value used in eq. 81. It can be passed
    in or calculated from the cross product of the velocity and the magnetic
    field in U.

    Note: the slightly messy keyword arguments are to keep numba happy - it
    doesn't like it if a keyword argument is a different type (e.g. None) to
    what it infers the variable to be from elsewhere within the function
    (e.g. an array).
    """

    qx, qy, nvar = U.shape

    Er = np.zeros((qx, qy))
    Ex = np.zeros((qx, qy))  # x-edges
    Ey = np.zeros((qx, qy))  # y-edges

    Ec = np.zeros((qx, qy))  # corner, (i,j) -> i-1/2, j-1/2

    dEdy_14 = np.zeros((qx, qy))  # (dE_z / dy)_(i, j-1/4)
    dEdx_14 = np.zeros((qx, qy))  # (dE_z / dx)_(i-1/4, j)

    dEdy_34 = np.zeros((qx, qy))  # (dE_z / dy)_(i, j-3/4)
    dEdx_34 = np.zeros((qx, qy))  # (dE_z / dx)_(i-3/4, j)

    nx = qx - 2 * ng
    ny = qy - 2 * ng
    ilo = ng
    ihi = ng + nx
    jlo = ng
    jhi = ng + ny

    if not use_ref:
        u = U[:, :, ixmom] / U[:, :, idens]
        v = U[:, :, iymom] / U[:, :, idens]
        bx = U[:, :, ixmag]
        by = U[:, :, iymag]
        Er[:, :] = -(u * by - v * bx)
    else:
        Er[:, :] = Eref

    # GS05 section 4.1.1
    Ex[:, :] = -Fx[:, :, iymag]
    Ey[:, :] = Fy[:, :, ixmag]

    for i in range(ilo - 3, ihi + 3):
        for j in range(jlo - 3, jhi + 3):

            # get the -1/4 states
            dEdy_14[i, j] = 2 * (Er[i, j] - Ey[i, j]) / dy

            dEdx_14[i, j] = 2 * (Er[i, j] - Ex[i, j]) / dx

            # get the -3/4 states
            dEdy_34[i, j] = 2 * (Ey[i, j] - Er[i, j - 1]) / dy

            dEdx_34[i, j] = 2 * (Ex[i, j] - Er[i - 1, j]) / dx

    for i in range(ilo - 2, ihi + 2):
        for j in range(jlo - 2, jhi + 2):

            # now get the corner states
            # this depends on the sign of the mass flux
            ru = Fx[i, j, idens]  # as Fx(i,j,idens) = (rho * vx)_i-1/2,j
            if ru > 0:
                dEdyx_14 = dEdy_14[i - 1, j]  # dEz/dy_(i-1/2,j-1/4)
            elif ru < 0:
                dEdyx_14 = dEdy_14[i, j]
            else:
                dEdyx_14 = 0.5 * (dEdy_14[i - 1, j] + dEdy_14[i, j])

            ru = Fx[i, j - 1, idens]  # as Fx(i,j,idens) = (rho * vx)_i-1/2,j-1
            if ru > 0:
                dEdyx_34 = dEdy_34[i - 1, j]  # dEz/dy_(i-1/2,j-3/4)
            elif ru < 0:
                dEdyx_34 = dEdy_34[i, j]
            else:
                dEdyx_34 = 0.5 * (dEdy_34[i - 1, j] + dEdy_34[i, j])

            rv = Fy[i, j, idens]
            if rv > 0:
                dEdxy_14 = dEdx_14[i, j - 1]  # dEz/dx_(i-1/4,j-1/2)
            elif rv < 0:
                dEdxy_14 = dEdx_14[i, j]
            else:
                dEdxy_14 = 0.5 * (dEdx_14[i, j - 1] + dEdx_14[i, j])

            rv = Fy[i - 1, j, idens]
            if rv > 0:
                dEdxy_34 = dEdx_34[i, j - 1]  # dEz/dx_(i-3/4,j-1/2)
            elif rv < 0:
                dEdxy_34 = dEdx_34[i, j]
            else:
                dEdxy_34 = 0.5 * (dEdx_34[i, j - 1] + dEdx_34[i, j])

            Ec[i, j] = 0.25 * (Ex[i, j] + Ex[i, j + 1] + Ey[i, j] + Ey[i + 1, j]) + \
                0.125 * dy * (dEdyx_14 - dEdyx_34) + \
                0.125 * dx * (dEdxy_14 - dEdxy_34)

    return Ec


# @njit(cache=True)
def sources(idir, ng, idens, ixmom, iymom, iener, ixmag, iymag, irhoX, dx, U, Ux):
    r"""
    Calculate source terms on the idir-interface. U is the cell-centered state,
    Ux should be a state on the idir-interface, where i,j -> i-1/2 ,j (for idir==1).

    Assume Bz = vz = 0 so that iener and iBz sources are 0.
    """
    qx, qy, nvar = U.shape

    S = np.zeros((qx, qy, nvar))

    nx = qx - 2 * ng
    ny = qy - 2 * ng
    ilo = ng
    ihi = ng + nx
    jlo = ng
    jhi = ng + ny

    for i in range(ilo - ng + 1, ihi + ng - 1):
        for j in range(jlo - ng + 1, jhi + ng - 1):
            if idir == 1:
                S[i, j, ixmom] = U[i, j, ixmag] * \
                    (Ux[i + 1, j, ixmag] - Ux[i, j, ixmag]) / dx
                S[i, j, iymom] = U[i, j, iymag] * \
                    (Ux[i + 1, j, ixmag] - Ux[i, j, ixmag]) / dx
            else:
                S[i, j, ixmom] = U[i, j, ixmag] * \
                    (Ux[i, j + 1, iymag] - Ux[i, j, iymag]) / dx
                S[i, j, iymom] = U[i, j, iymag] * \
                    (Ux[i, j + 1, iymag] - Ux[i, j, iymag]) / dx

    return S

# @njit(cache=True)
# def artificial_viscosity(ng, dx, dy,
#                          cvisc, u, v):
#     r"""
#     Compute the artifical viscosity.  Here, we compute edge-centered
#     approximations to the divergence of the velocity.  This follows
#     directly Colella \ Woodward (1984) Eq. 4.5
#
#     data locations::
#
#         j+3/2--+---------+---------+---------+
#                |         |         |         |
#           j+1  +         |         |         |
#                |         |         |         |
#         j+1/2--+---------+---------+---------+
#                |         |         |         |
#              j +         X         |         |
#                |         |         |         |
#         j-1/2--+---------+----Y----+---------+
#                |         |         |         |
#            j-1 +         |         |         |
#                |         |         |         |
#         j-3/2--+---------+---------+---------+
#                |    |    |    |    |    |    |
#                    i-1        i        i+1
#              i-3/2     i-1/2     i+1/2     i+3/2
#
#     ``X`` is the location of ``avisco_x[i,j]``
#     ``Y`` is the location of ``avisco_y[i,j]``
#
#     Parameters
#     ----------
#     ng : int
#         The number of ghost cells
#     dx, dy : float
#         Cell spacings
#     cvisc : float
#         viscosity parameter
#     u, v : ndarray
#         x- and y-velocities
#
#     Returns
#     -------
#     out : ndarray, ndarray
#         Artificial viscosity in the x- and y-directions
#     """
#
#     qx, qy = u.shape
#
#     avisco_x = np.zeros((qx, qy))
#     avisco_y = np.zeros((qx, qy))
#
#     nx = qx - 2 * ng
#     ny = qy - 2 * ng
#     ilo = ng
#     ihi = ng + nx
#     jlo = ng
#     jhi = ng + ny
#
#     for i in range(ilo - 1, ihi + 1):
#         for j in range(jlo - 1, jhi + 1):
#
#                 # start by computing the divergence on the x-interface.  The
#                 # x-difference is simply the difference of the cell-centered
#                 # x-velocities on either side of the x-interface.  For the
#                 # y-difference, first average the four cells to the node on
#                 # each end of the edge, and: difference these to find the
#                 # edge centered y difference.
#             divU_x = (u[i, j] - u[i - 1, j]) / dx + \
#                 0.25 * (v[i, j + 1] + v[i - 1, j + 1] -
#                         v[i, j - 1] - v[i - 1, j - 1]) / dy
#
#             avisco_x[i, j] = cvisc * max(-divU_x * dx, 0.0)
#
#             # now the y-interface value
#             divU_y = 0.25 * (u[i + 1, j] + u[i + 1, j - 1] - u[i - 1, j] - u[i - 1, j - 1]) / dx + \
#                 (v[i, j] - v[i, j - 1]) / dy
#
#             avisco_y[i, j] = cvisc * max(-divU_y * dy, 0.0)
#
#     return avisco_x, avisco_y
