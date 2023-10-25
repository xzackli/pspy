"""Tests pspy versus namaster."""
import os
import tempfile
import time
import unittest

import healpy as hp
import numpy as np
import pspy
from pspy import pspy_utils, so_map, so_mcm, so_spectra, so_window, sph_tools


def _assert_import(pkg_name):
    try:
        import importlib

        importlib.import_module(pkg_name)
    except ModuleNotFoundError:
        raise ModuleNotFoundError(f"You need to install '{pkg_name}' to use this unit test")


_assert_import("camb")
_assert_import("pymaster")

import camb
import pymaster as nmt

output_dir = os.path.join(tempfile.gettempdir(), "test_pspy_namaster")
os.makedirs(output_dir, exist_ok=True)

lmin, lmax = 2, 10**4
l = np.arange(lmin, lmax)
cosmo_params = {
    "H0": 67.5,
    "As": 1e-10 * np.exp(3.044),
    "ombh2": 0.02237,
    "omch2": 0.1200,
    "ns": 0.9649,
    "Alens": 1.0,
    "tau": 0.0544,
}
pars = camb.set_params(**cosmo_params)
pars.set_for_lmax(lmax, lens_potential_accuracy=1)
results = camb.get_results(pars)
powers = results.get_cmb_power_spectra(pars, CMB_unit="muK")
cl_file = os.path.join(output_dir, "cl_camb.dat")
np.savetxt(cl_file, np.hstack([l[:, np.newaxis], powers["total"][lmin:lmax]]))

lon, lat = 30, 50
radius = 25
nside = 512
lmax = 3 * nside - 1
ncomp = 3
niter = 3
spectra = ["TT", "TE", "TB", "ET", "BT", "EE", "EB", "BE", "BB"]

template_healpix = so_map.healpix_template(ncomp, nside=nside)

binary_healpix = so_map.healpix_template(ncomp=1, nside=nside)
vec = hp.ang2vec(lon, lat, lonlat=True)
disc = hp.query_disc(nside, vec, radius=np.deg2rad(radius))
binary_healpix.data[disc] = 1

cmb = template_healpix.synfast(cl_file)
noise = so_map.white_noise(cmb, rms_uKarcmin_T=20, rms_uKarcmin_pol=np.sqrt(2) * 20)
cmb.data += noise.data

window = so_window.create_apodization(binary_healpix, apo_type="C1", apo_radius_degree=1)
mask = so_map.simulate_source_mask(binary_healpix, n_holes=10, hole_radius_arcmin=30)
mask = so_window.create_apodization(mask, apo_type="C1", apo_radius_degree=1)
window.data *= mask.data
window = (window, window)


def run_pspy(pure=False):
    binning_file = os.path.join(output_dir, "binning.dat")
    pspy_utils.create_binning_file(bin_size=40, n_bins=100, file_name=binning_file)
    mbb_inv, Bbl = so_mcm.mcm_and_bbl_spin0and2(
        window, binning_file, lmax=lmax, type="Cl", niter=niter, pure=pure
    )
    get_alms = sph_tools.get_pure_alms if pure else sph_tools.get_alms
    alms = get_alms(cmb, window, niter=niter, lmax=lmax)
    ell, ps = so_spectra.get_spectra(alms, spectra=spectra)
    lb, Clb = so_spectra.bin_spectra(
        ell, ps, binning_file, lmax, type="Cl", mbb_inv=mbb_inv, spectra=spectra
    )
    return lb, Clb


def run_namaster(pure=False):
    nlb = 40
    b = nmt.NmtBin(nside, nlb=nlb)
    lb = b.get_effective_ells()
    if pure:
        field = nmt.NmtField(
            window[1].data,
            [cmb.data[1], cmb.data[2]],
            purify_e=True,
            purify_b=True,
            n_iter_mask_purify=niter,
            n_iter=niter,
        )
        wsp = nmt.NmtWorkspace()
        wsp.compute_coupling_matrix(field, field, b, n_iter=niter)
        cls_coupled = nmt.compute_coupled_cell(field, field)
        cls_uncoupled = wsp.decouple_cell(cls_coupled)
        Clb = {"BB": cls_uncoupled[3]}
    else:
        field_0 = nmt.NmtField(window[0].data, [cmb.data[0]])
        field_2 = nmt.NmtField(window[1].data, [cmb.data[1], cmb.data[2]])
        wsp = nmt.NmtWorkspace()
        wsp.compute_coupling_matrix(field_0, field_2, b, is_teb=True, n_iter=niter, lmax_mask=lmax)
        cl_coupled_00 = nmt.compute_coupled_cell(field_0, field_0)
        cl_coupled_02 = nmt.compute_coupled_cell(field_0, field_2)
        cl_coupled_22 = nmt.compute_coupled_cell(field_2, field_2)
        cls_coupled = np.array(
            [
                cl_coupled_00[0],  # TT
                cl_coupled_02[0],  # TE
                cl_coupled_02[1],  # TB
                cl_coupled_22[0],  # EE
                cl_coupled_22[1],  # EB
                cl_coupled_22[2],  # BE
                cl_coupled_22[3],  # BB
            ]
        )
        cls_uncoupled = wsp.decouple_cell(cls_coupled)
        Clb = {
            k: cls_uncoupled[i] for i, k in enumerate(["TT", "TE", "TB", "EE", "EB", "BE", "BB"])
        }
        Clb["ET"] = Clb["TE"]
        Clb["BT"] = Clb["TB"]
    return lb, Clb


class SOPspyNamasterTests(unittest.TestCase):
    def setUp(self):
        print(f"pspy current version : {pspy.__version__}")
        # This sould be fixed see https://github.com/LSSTDESC/NaMaster/issues/172
        # print(f"namaster current version : {nmt.__version__}")
        pass

    def test_pspy_namaster(self):
        t0 = time.time()
        lb_pspy, Clb_pspy = run_pspy()
        print(f"pspy runs in {time.time() - t0:.2f} seconds")

        t0 = time.time()
        lb_nmt, Clb_nmt = run_namaster()
        print(f"namaster runs in {time.time() - t0:.2f} seconds")

        decimal = 12
        for spec in spectra:
            msg = f"Testing {spec} spectrum"
            np.testing.assert_almost_equal(
                Clb_pspy[spec], Clb_nmt[spec], err_msg=msg, decimal=decimal
            )

    def test_pspy_namaster_pure(self):
        t0 = time.time()
        lb_pspy, Clb_pspy = run_pspy(pure=True)
        print(f"pspy (pure) runs in {time.time() - t0:.2f} seconds")

        t0 = time.time()
        lb_nmt, Clb_nmt = run_namaster(pure=True)
        print(f"namaster (pure) runs in {time.time() - t0:.2f} seconds")

        np.testing.assert_almost_equal(
            Clb_pspy["BB"], Clb_nmt["BB"], err_msg=f"Testing BB spectrum", decimal=12
        )


if __name__ == "__main__":
    unittest.main()
