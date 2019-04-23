#  PyTransit: fast and easy exoplanet transit modelling in Python.
#  Copyright (C) 2010-2019  Hannu Parviainen
#
#  This program is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
#  This program is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program.  If not, see <https://www.gnu.org/licenses/>.


"""Mandel-Agol transit model


.. moduleauthor:: Hannu Parviainen <hannu.parviainen@astro.ox.ac.uk>
"""

import numpy as np
import pyopencl as cl
from os.path import dirname, join

from numpy import array, uint32, float32, int32, asarray, zeros, ones, unique

from .numba.ma_quadratic_nb import calculate_interpolation_tables
from .transitmodel import TransitModel


class QuadraticModelCL(TransitModel):
    """
    Exoplanet transit light curve model by Mandel and Agol (2001).

    :param nldc: (optional)
        Number of limb darkening coefficients (1 = linear limb darkening, 2 = quadratic)

    :param supersampling: (optional)
        Number of subsamples to calculate for each light curve point

    :param exptime: (optional)
        Integration time for a single exposure, used in supersampling


    Examples
    --------

    Basic case::

      m = MandelAgolCL() # Initialize the model, use quadratic limb darkening law and all available cores
      I = m(z,k,u)       # Evaluate the model for projected distance z, radius ratio k, and limb darkening coefficients u
      
    """

    def __init__(self, npb: int = 1, eccentric: bool = False, constant_k: bool = True,
                 contamination: bool = False, optimize: bool = False, eclipse: bool = False, supersampling: int = 1,
                 exptime: float = 0.020433598, klims: tuple = (0.05, 0.25), nk: int = 256, nz: int = 256,
                 cl_ctx=None, cl_queue=None):
        super().__init__(npb, eccentric, constant_k, contamination=contamination, optimize=optimize, eclipse=eclipse)

        self.ctx = cl_ctx or cl.create_some_context()
        self.queue = cl_queue or cl.CommandQueue(self.ctx)

        self.ed,self.le,self.ld,self.kt,self.zt = map(lambda a: np.array(a,dtype=float32,order='C'),
                                                      calculate_interpolation_tables(klims[0],klims[1],nk,nz))
        self.klims = klims
        self.nk    = int32(nk)
        self.nz    = int32(nz)
        self.nptb  = 0
        self.npb   = 0
        self.u     = np.array([])
        self.f     = None
        self.k0, self.k1 = map(float32, self.kt[[0,-1]])
        self.dk = float32(self.kt[1]-self.kt[0])
        self.dz = float32(self.zt[1]-self.zt[0])
        self.nss = int32(supersampling)
        self.etime = float32(exptime)

        self.pv = array([])

        mf = cl.mem_flags

        # Create the buffers for the Mandel & Agol coefficient arrays
        self._b_ed = cl.Buffer(self.ctx, mf.READ_ONLY | mf.COPY_HOST_PTR, hostbuf=self.ed)
        self._b_le = cl.Buffer(self.ctx, mf.READ_ONLY | mf.COPY_HOST_PTR, hostbuf=self.le)
        self._b_ld = cl.Buffer(self.ctx, mf.READ_ONLY | mf.COPY_HOST_PTR, hostbuf=self.ld)
        self._b_kt = cl.Buffer(self.ctx, mf.READ_ONLY | mf.COPY_HOST_PTR, hostbuf=self.kt)
        self._b_zt = cl.Buffer(self.ctx, mf.READ_ONLY | mf.COPY_HOST_PTR, hostbuf=self.zt)

        self.time  = None
        self.lcids = None
        self.pbids = None
        self.nsamples = None
        self.exptimes = None

        # Declare the buffers for the ld coefficients, time, and flux arrays. These will
        # be initialised when the model is first evaluated, and reinitialised if the
        # array sizes change.
        #
        self._b_u = None       # Limb darkening coefficient buffer
        self._b_time = None       # Time buffer
        self._b_f = None       # Flux buffer
        self._b_p = None       # Parameter vector buffer

        self._time_id = None   # Time array ID

        self.prg = cl.Program(self.ctx, open(join(dirname(__file__),'opencl','ma_cuadratic.cl'),'r').read()).build()


    def set_data(self, time, lcids=None, pbids=None, nsamples=None, exptimes=None):
        mf = cl.mem_flags

        if self._b_time is not None:
            self._b_time.release()
            self._b_lcids.release()
            self._b_pbids.release()
            self._b_nsamples.release()
            self._b_etimes.release()

        self.nlc = uint32(1 if lcids is None else unique(lcids).size)
        self.npb = uint32(1 if pbids is None else unique(pbids).size)
        self.nptb = time.size

        self.time = asarray(time, dtype='float32')
        self.lcids = zeros(t.size, 'uint32') if lcids is None else asarray(lcids, dtype='uint32')
        self.pbids = zeros(self.nlc, 'uint32') if pbids is None else asarray(pbids, dtype='uint32')
        self.nsamples = ones(self.nlc, 'uint32') if nsamples is None else asarray(nsamples, dtype='uint32')
        self.exptimes = ones(self.nlc, 'float32') if exptimes is None else asarray(exptimes, dtype='float32')

        self._b_time = cl.Buffer(self.ctx, mf.READ_ONLY | mf.COPY_HOST_PTR, hostbuf=self.time)
        self._b_lcids = cl.Buffer(self.ctx, mf.READ_ONLY | mf.COPY_HOST_PTR, hostbuf=self.lcids)
        self._b_pbids = cl.Buffer(self.ctx, mf.READ_ONLY | mf.COPY_HOST_PTR, hostbuf=self.pbids)
        self._b_nsamples = cl.Buffer(self.ctx, mf.READ_ONLY | mf.COPY_HOST_PTR, hostbuf=self.nsamples)
        self._b_etimes = cl.Buffer(self.ctx, mf.READ_ONLY | mf.COPY_HOST_PTR, hostbuf=self.exptimes)


    def evaluate_ps(self, k, ldc, t0, p, a, i, e=0., w=0., copy=True):
        ldc = np.array(ldc, float32, order='C').T

        # Release and reinitialise the GPU buffers if the sizes of the time or
        # limb darkening coefficient arrays change.
        if (ldc.size != self.u.size):
            if self._b_u is not None:
                self._b_f.release()
                self._b_u.release()
                self._b_p.release()

            self.npb = 1 if ldc.ndim == 1 else ldc.shape[1]
            self.nptb = t.size

            self.u = np.zeros((2,self.npb), float32)
            self.f = np.zeros((self.nptb, self.npb), float32)
            self.pv = np.zeros(7, float32)

            mf = cl.mem_flags
            self._b_time = cl.Buffer(self.ctx, mf.READ_ONLY | mf.COPY_HOST_PTR, hostbuf=t)
            self._b_f = cl.Buffer(self.ctx, mf.WRITE_ONLY, t.nbytes)
            self._b_u = cl.Buffer(self.ctx, mf.READ_ONLY | mf.COPY_HOST_PTR, hostbuf=self.u)
            self._b_p = cl.Buffer(self.ctx, mf.READ_ONLY | mf.COPY_HOST_PTR, hostbuf=self.pv)

        # Copy the time array to the GPU if it has been changed
        if id(t) != self._time_id:
            cl.enqueue_copy(self.queue, self._b_time, t)
            self._time_id = id(t)

        # Copy the limb darkening coefficient array to the GPU
        cl.enqueue_copy(self.queue, self._b_u, ldc)

        # Copy the parameter vector to the GPU
        self.pv[:] = np.array([k, t0, p, a, i, e, w], dtype=float32)
        cl.enqueue_copy(self.queue, self._b_p, self.pv)

        self.prg.ma_eccentric(self.queue, t.shape, None, self._b_time, self._b_p, self._b_u,
                              self._b_ed, self._b_le, self._b_ld, self.nss, self.etime,
                              self.k0, self.k1, self.nk, self.nz, self.dk, self.dz, self._b_f)

        if copy:
            cl.enqueue_copy(self.queue, self.f, self._b_f)
            return self.f
        else:
            return None


    def evaluate_pv(self, pvp, ldc, copy=True):
        ldc = asarray(ldc, float32)
        self.npv = uint32(pvp.shape[0])
        self.spv = uint32(pvp.shape[1])

        # Release and reinitialise the GPU buffers if the sizes of the time or
        # limb darkening coefficient arrays change.
        if (ldc.size != self.u.size) or (pvp.size != self.pv.size):
            assert self.npb == ldc.shape[1] // 2

            if self._b_f is not None:
                self._b_f.release()
                self._b_u.release()
                self._b_p.release()

            self.pv = zeros(pvp.shape, float32)
            self.u = zeros((self.npv, 2 * self.npb), float32)
            self.f = zeros((self.npv, self.nptb), float32)

            mf = cl.mem_flags
            self._b_f = cl.Buffer(self.ctx, mf.WRITE_ONLY, self.time.nbytes * self.npv)
            self._b_u = cl.Buffer(self.ctx, mf.READ_ONLY | mf.COPY_HOST_PTR, hostbuf=self.u)
            self._b_p = cl.Buffer(self.ctx, mf.READ_ONLY | mf.COPY_HOST_PTR, hostbuf=self.pv)

        # Copy the limb darkening coefficient array to the GPU
        cl.enqueue_copy(self.queue, self._b_u, ldc)

        # Copy the parameter vector to the GPU
        self.pv[:] = pvp
        cl.enqueue_copy(self.queue, self._b_p, self.pv)

        self.prg.ma_eccentric_pop(self.queue, (self.npv, self.nptb), None, self._b_time, self._b_lcids, self._b_pbids,
                                  self._b_p, self._b_u,
                                  self._b_ed, self._b_le, self._b_ld, self._b_nsamples, self._b_etimes,
                                  self.k0, self.k1, self.nk, self.nz, self.dk, self.dz,
                                  self.spv, self.nlc, self.npb, self._b_f)

        if copy:
            cl.enqueue_copy(self.queue, self.f, self._b_f)
            return self.f
        else:
            return None


    def evaluate_pv_ttv(self, t, pvp, ldc, tids, ntr, copy=True, tdv=False):
        ldc = asarray(ldc, float32)
        self.npv = uint32(pvp.shape[0])
        self.spv = uint32(pvp.shape[1])
        tids = asarray(tids, 'int32')
        ntr = uint32(ntr)

        # Release and reinitialise the GPU buffers if the sizes of the time or
        # limb darkening coefficient arrays change.
        if (t.size != self.nptb) or (ldc.size != self.u.size) or (pvp.size != self.pv.size):

            if self._b_time is not None:
                self._b_time.release()
                self._b_f.release()
                self._b_u.release()
                self._b_p.release()

            self.npb = 1 if ldc.ndim == 1 else ldc.shape[0]
            self.nptb = t.size

            self.pv = zeros(pvp.shape, float32)
            self.u  = zeros((self.npb, 2), float32)
            self.f  = zeros((self.npv, t.size), float32)

            mf = cl.mem_flags
            self._b_time   = cl.Buffer(self.ctx, mf.READ_ONLY | mf.COPY_HOST_PTR, hostbuf=t)
            self._b_f   = cl.Buffer(self.ctx, mf.WRITE_ONLY, t.nbytes * self.npv)
            self._b_u   = cl.Buffer(self.ctx, mf.READ_ONLY | mf.COPY_HOST_PTR, hostbuf=self.u)
            self._b_p   = cl.Buffer(self.ctx, mf.READ_ONLY | mf.COPY_HOST_PTR, hostbuf=self.pv)
            self._b_tid = cl.Buffer(self.ctx, mf.READ_ONLY | mf.COPY_HOST_PTR, hostbuf=tids)

        # Copy the time array to the GPU if it has been changed
        if id(t) != self._time_id:
            cl.enqueue_copy(self.queue, self._b_time, t)
            cl.enqueue_copy(self.queue, self._b_tid, tids)
            self._time_id = id(t)

        # Copy the limb darkening coefficient array to the GPU
        cl.enqueue_copy(self.queue, self._b_u, ldc)

        # Copy the parameter vector to the GPU
        self.pv[:] = pvp
        cl.enqueue_copy(self.queue, self._b_p, self.pv)

        if tdv:
            self.prg.ma_eccentric_pop_tdv(self.queue, (self.npv, t.size), None, self._b_time, self._b_p, self._b_u,
                                          self._b_tid, ntr, self._b_ed, self._b_le, self._b_ld, self.nss, self.etime,
                                          self.k0, self.k1, self.nk, self.nz, self.dk, self.dz, self.spv, self._b_f)
        else:
            self.prg.ma_eccentric_pop_ttv(self.queue, (self.npv, t.size), None, self._b_time, self._b_p, self._b_u,
                                          self._b_tid, ntr, self._b_ed, self._b_le, self._b_ld, self.nss, self.etime,
                                          self.k0, self.k1, self.nk, self.nz, self.dk, self.dz, self.spv, self._b_f)

        if copy:
            cl.enqueue_copy(self.queue, self.f, self._b_f)
            return self.f
        else:
            return None