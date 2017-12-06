#! /usr/bin/env python
# -*- coding: utf-8 -*-


import warnings
import numpy as np
import cPickle
from copy import copy, deepcopy
from collections import OrderedDict as odict
from itertools import izip

import sncosmo
from sncosmo.utils         import dict_to_array
from astropy.table         import Table, vstack, hstack

from propobject import BaseObject

from utils.tools   import kwargs_update, range_args, range_length, get_progressbar
from utils.skybins import SurveyField, SurveyFieldBins 

_d2r = np.pi/180

__all__ = ["SimulSurvey", "SurveyPlan", "LightcurveCollection"]

#######################################
#                                     #
# Survey: Simulation Base             #
#                                     #
#######################################
class SimulSurvey( BaseObject ):
    """
    Basic survey object
    """
    __nature__ = "SimulSurvey"

    PROPERTIES         = ["generator", "instruments", "plan"]
    SIDE_PROPERTIES    = ["pointings", "blinded_bias", "phase_range"]
    DERIVED_PROPERTIES = ["obs_fields", "obs_ccd",
                          "non_field_obs", "non_field_obs_ccd",
                          "non_field_obs_exist"]

    def __init__(self, generator=None, plan=None,
                 instprop=None, blinded_bias=None,
                 phase_range=None, empty=False):
        """
        Parameters:
        ----------
        generator: [simultarget.transient_generator or derived child like sn_generator]

        """
        self.__build__()
        if empty:
            return

        self.create(generator, plan, instprop, blinded_bias, phase_range)

    def create(self, generator, plan, instprop, blinded_bias, phase_range):
        """
        """
        if generator is not None:
            self.set_target_generator(generator)

        if plan is not None:
            self.set_plan(plan)
            self.set_instruments(instprop)

        if blinded_bias is not None:
            self.set_blinded_bias(blinded_bias)

        if phase_range is not None:
            self.set_phase_range(phase_range)

    # =========================== #
    # = Main Methods            = #
    # =========================== #

    # ---------------------- #
    # - Get Methods        - #
    # ---------------------- #
    def get_lightcurves(self, *args, **kwargs):
        """
        """
        args = range_args(self.generator.ntransient, *args)
        progress_bar = kwargs.pop('progress_bar', False)
        notebook = kwargs.pop('notebook', False)

        if not self.is_set():
            raise AttributeError("plan, generator or instrument not set")

        lcs = LightcurveCollection(empty=True)
        gen = izip(xrange(*args),
                   self.generator.get_lightcurve_full_param(*args),
                   self._get_observations_(*args))

        progress_bar_success = False
        
        if progress_bar:
            self._assign_obs_fields_(progress_bar=True, notebook=notebook)
            self._assign_non_field_obs_(progress_bar=True, notebook=notebook)
            
            try:
                print 'Generating lightcurves'
                ntransient = range_length(*args)

                with get_progressbar(ntransient, notebook=notebook) as bar:
                    for k, p, obs in gen:
                        if obs is not None:
                            lcs.add(self._get_lightcurve_(p, obs, k))
                            bar.update()
                        else:
                            bar.update()

                progress_bar_success = True
            except ImportError:
                progress_bar_success = False
            except IOError:
                progress_bar_success = False

        if not progress_bar_success:
            for k, p, obs in gen:
                if obs is not None:
                    lcs.add(self._get_lightcurve_(p, obs, k))

        return lcs
            
    def _get_lightcurve_(self, p, obs, idx_orig=None):
        """
        """        
        if obs is not None:
            ra, dec, mwebv_sfd98 = p.pop('ra'), p.pop('dec'), p.pop('mwebv_sfd98')

            # Get unperturbed lc from sncosmo
            lc = sncosmo.realize_lcs(obs, self.generator.model, [p],
                                     scatter=False)[0]

            # Make sure that lc points outside model definition are zero
            self.generator.model.set(**p)
            outside = ((lc['time'] < self.generator.model.mintime()) |
                       (lc['time'] > self.generator.model.maxtime()))
            lc['flux'][outside] = 0.

            for col_ in ['field', 'ccd', 'comment']:
                if col_ in obs.colnames:
                    lc = hstack((lc, obs[(col_,)]))

            # Replace fluxerrors with covariance matrix that contains
            # correlated terms for the calibration uncertainty
            fluxerr = np.sqrt(obs['skynoise']**2 +
                              np.abs(lc['flux']) / obs['gain'])

            fluxcov = np.diag(fluxerr**2)
            save_cov = False
            for band in set(obs['band']):
                if self.instruments[band]['err_calib'] is not None:
                    save_cov = True
                    idx = np.where(obs['band'] == band)[0]
                    err = self.instruments[band]['err_calib']
                    for k0 in idx:
                        for k1 in idx:
                            fluxcov[k0,k1] += (lc['flux'][k0] * 
                                               lc['flux'][k1] *
                                               err**2)

            # Add random (but correlated) noise to the fluxes
            fluxchol = np.linalg.cholesky(fluxcov)
            flux = lc['flux'] + fluxchol.dot(np.random.randn(len(lc)))

            # Apply blinded bias if given
            if self.blinded_bias is not None:
                bias_array = np.array([self.blinded_bias[band]
                                       if band in self.blinded_bias.keys() else 0
                                       for band in obs['band']])
                flux *= 10 ** (-0.4*bias_array)

            lc['flux'] = flux
            lc['fluxerr'] = np.sqrt(np.diag(fluxcov))

            # Additional metadata for the lc fitter
            lc.meta['ra'] = ra
            lc.meta['dec'] = dec
            if save_cov:
                lc.meta['fluxcov'] = fluxcov
            lc.meta['mwebv_sfd98'] = mwebv_sfd98
            if idx_orig is not None:
                lc.meta['idx_orig'] = idx_orig
        else:
            lc = None

        return lc

    # ---------------------- #
    # - Setter Methods     - #
    # ---------------------- #

    # -------------
    # - Targets
    def set_target_generator(self, generator):
        """
        """
        if "__nature__" not in dir(generator) or\
          generator.__nature__ != "TransientGenerator":
            raise TypeError("generator must be an astrobject TransientGenerator")

        if not generator.has_lightcurves():
            warnings.warn("No lightcurves set in the given transient generator")

        self._properties["generator"] = generator

    # -------------
    # - SurveyPlan
    def set_plan(self,plan):
        """
        """
        # ----------------------
        # - Load cadence here
        if "__nature__" not in dir(plan) or \
          plan.__nature__ != "SurveyPlan":
            raise TypeError("the input 'plan' must be an astrobject SurveyPlan")
        self._properties["plan"] = plan

        # ----------------------------
        # - Set back the observations
        # self._reset_observations_()
        self._reset_obs_fields_()
        self._reset_non_field_obs_()

    # -------------
    # - Instruments
    def set_instruments(self, properties):
        """
        properties must be a dictionary containing the
        instruments' information (bandname,gain,zp,zpsys,err_calib) related
        to each bands


        example..
        ---------
        properties = {"desg":{"gain":1,"zp":30,"zpsys":'ab',"err_calib":0.005},
                      "desr":{"gain":1,"zp":30,"zpsys":'ab',"err_calib":0.005}}
        """
        prop = deepcopy(properties)
    
        if prop is not None:
            for band, d_ in prop.items():
                gain = d_.pop("gain", 1.)
                zp = d_.pop("zp", None)
                zpsys = d_.pop("zpsys","ab")
                err_calib = d_.pop("err_calib", None)
                if gain is None or zp is None:
                    raise ValueError('gain or zp is None or not defined for %s'%band)
                self.add_instrument(band, gain, zp, zpsys, err_calib,
                                    update=False, **d_)

        for b_ in np.unique(self.pointings["band"]):
            if b_ not in self.instruments.keys():
                self.add_instrument(b_, update=False)

        #self._reset_observations_()

    # -----------------------
    # - Blinded bias in bands
    def set_blinded_bias(self, bias):
        """Expect input dict of band and bounds maximum bias
        Bias will be drawn from uniform distribution
        """
        self._side_properties['blinded_bias'] = {k: np.random.uniform(-v, v) 
                                            for k, v in bias.items()}

    def set_phase_range(self, phase_range):
        """
        """
        if len(phase_range) != 2:
            raise ValueError('phase_range must contain exactly two floats')
        self._side_properties['phase_range'] = phase_range

    # ---------------------- #
    # - Add Stuffs         - #
    # ---------------------- #
    def add_instrument(self, bandname, gain=1., zp=30, zpsys="ab", err_calib=None,
                       force_it=True, update=True, **kwargs):
        """
        kwargs could be any properties you wish to save with the instrument
        """
        if self._properties["instruments"] is None:
            self._properties["instruments"] = {}

        if bandname in self.instruments.keys() and not force_it:
            raise AttributeError("%s is already defined."+\
                                 " Set force_it to True to overwrite it. ")

        instprop = {"gain": gain,
                    "zp": zp,
                    "zpsys": zpsys,
                    "err_calib": err_calib}
        self.instruments[bandname] = kwargs_update(instprop,**kwargs)

        if update:
            # self._reset_observations_()
            pass

    # ---------------------- #
    # - Recover Methods    - #
    # ---------------------- #
    #def recover_targets(self):
    #    """
    #    bunch threshold...
    #    """
    #
    #def recover_lightcurves(self):
    #    """
    #    """

    # =========================== #
    # = Internal Methods        = #
    # =========================== #
    def _update_lc_(self):
        """
        """
        # -----------------------------
        # -- Do you have all you need ?
        if not self.is_set():
            return
            
    def _get_observations_(self, *args):
        """
        """  
        # -------------
        # - Input test
        if self.plan is None or self.instruments is None:
            raise AttributeError("Plan or Instruments is not set.")

        # -----------------------
        # - Check if instruments exists
        all_instruments = np.unique(self.pointings["band"])
        if not np.all([i in self.instruments.keys() for i in all_instruments]):
            raise ValueError("Some of the instrument in cadence have not been defined."+"\n"+
                             "given instruments :"+", ".join(all_instruments.tolist())+"\n"+
                             "known instruments :"+", ".join(self.instruments.keys()))

        # -----------------------
        # - Based on the model get a reasonable time scale for each transient
        mjd = self.generator.mjd
        z = np.array(self.generator.zcmb)
        mjd_range = [mjd + self.phase_range[0] * (1+z), 
                     mjd + self.phase_range[1] * (1+z)]

        # -----------------------
        # - Let's build the tables
        for k in xrange(*range_args(self.generator.ntransient, *args)):
            obs = self.plan.observed_on(self.obs_fields[k],
                                        (self.obs_ccds[k]
                                         if self.obs_ccds is not None
                                         else None),
                                        self.non_field_obs[k],
                                        (self.non_field_obs_ccds[k]
                                         if self.non_field_obs_ccds is not None
                                         else None),
                                        (mjd_range[0][k], mjd_range[1][k]))

            data = [[self.instruments[b]["gain"] for b in obs["band"]],
                    [self.instruments[b]["zpsys"] for b in obs["band"]]]
            names = ["gain", "zpsys"]

            if 'zp' in obs.keys():
                mask = np.isnan(obs['zp'])
                obs['zp'][mask] =  [self.instruments[b]["zp"]
                                    for b in obs[mask]["band"]]
            else:
                data.append([self.instruments[b]["zp"]
                             for b in obs["band"]])
                name.append("zp")
            
            if len(obs) > 0:
                yield hstack((obs,Table(data=data, names=names)))
            else:
                yield None

    def _assign_obs_fields_(self, progress_bar=False, notebook=False):
        """
        """
        f, c = self.plan.get_obs_fields(
            self.generator.ra,
            self.generator.dec,
            field_id=np.unique(self.cadence['field']),
            progress_bar=progress_bar,
            notebook=notebook
        )
        self._derived_properties["obs_fields"] = f
        self._derived_properties["obs_ccds"] = c

    def _reset_obs_fields_(self):
        """
        """
        self._derived_properties["obs_fields"] = None
        self._derived_properties["obs_ccds"] = None

    def _assign_non_field_obs_(self, progress_bar=False, notebook=False):
        """
        """
        f, c = self.plan.get_non_field_obs(
            self.generator.ra,
            self.generator.dec,
            progress_bar=progress_bar,
            notebook=notebook
        )
        self._derived_properties["non_field_obs"] = f
        self._derived_properties["non_field_obs_ccds"] = c

    def _reset_non_field_obs_(self):
        """
        """
        self._derived_properties["non_field_obs"] = None
        self._derived_properties["non_field_obs_ccd"] = None
        self._derived_properties["non_field_obs_exist"] = None
    
    # =========================== #
    # = Properties and Settings = #
    # =========================== #
    @property
    def instruments(self):
        """The basic information relative to the instrument used for the survey"""
        if self._properties["instruments"] is None:
            return {}
        return self._properties["instruments"]

    @property
    def generator(self):
        """The instance that enable to create fake targets"""
        return self._properties["generator"]

    @property
    def plan(self):
        """This is the survey plan including field definitions and telescope pointings"""
        return self._properties["plan"]

    def is_set(self):
        """This parameter is True if this has plan, instruments and genetor set"""
        return not (self._properties["instruments"] is None or \
                    self.generator is None or \
                    self.plan is None)

    # ------------------
    # - Side properties
    @property
    def pointings(self):
        """This is a table containing where the telescope is pointed with which band."""
        if self._properties["plan"] is not None:
            return self._properties["plan"].pointings
        else:
            raise ValueError("Property 'plan' not set yet")

    @property
    def cadence(self):
        """This is a table containing where the telescope is pointed with which band."""
        warnings.warn("cadence has been renamed pointings", DeprecationWarning)
        if self._properties["plan"] is not None:
            return self._properties["plan"].pointings
        else:
            raise ValueError("Property 'plan' not set yet")

    @property
    def blinded_bias(self):
        """Blinded bias applied to specific bands for all observations"""
        return self._side_properties["blinded_bias"]

    @property
    def phase_range(self):
        """Phase range for lightcurve generation, default derived from model source
        with 14 rest-frame days prior to t0"""
        if self._side_properties["phase_range"] is not None:
            return self._side_properties["phase_range"]
        else:
            return (self.generator.model._source.minphase() - 14,
                    self.generator.model._source.maxphase())

    # ------------------
    # - Derived values
    @property
    def obs_fields(self):
        """Transients are assigned fields that they are found"""
        if self._derived_properties["obs_fields"] is None:
            self._assign_obs_fields_()

        return self._derived_properties["obs_fields"]

    @property
    def obs_ccds(self):
        """Transients are assigned fields that they are found"""
        if (self._derived_properties["obs_fields"] is None and
            self._derived_properties["obs_ccds"] is None and
            self.plan.ccds is not None):
            self._assign_obs_fields_()

        return self._derived_properties["obs_ccds"]

    @property
    def non_field_obs(self):
        """If the plan contains pointings with field id, prepare a list of those."""
        if (self._derived_properties["non_field_obs"] is None
            and self.non_field_obs_exist is False):
            self._assign_non_field_obs_()
            
        if self._derived_properties["non_field_obs"] is None:
            self._derived_properties["non_field_obs_exist"] = False
        else:
            self._derived_properties["non_field_obs_exist"] = True

        if self.non_field_obs_exist is False:
            return [None for k in xrange(self.generator.ntransient)]
        return self._derived_properties["non_field_obs"]

    @property
    def non_field_obs_ccds(self):
        """If the plan contains pointings with field id, prepare a list of those."""
        if self.non_field_obs_exist is False:
            return [None for k in xrange(self.generator.ntransient)]
        return self._derived_properties["non_field_obs_ccds"]
        
    @property
    def non_field_obs_exist(self):
        """Avoid checking for non-field pointings more than once."""
        return self._derived_properties["non_field_obs_exist"]

#######################################
#                                     #
# Survey: Plan object                 #
#                                     #
#######################################
class SurveyPlan( BaseObject ):
    """
    Survey Plan
    contains the list of observation times, bands and pointings and
    can return that times and bands, which a transient is observed at/with.
    A list of fields can be given to simplify adding observations and avoid 
    lookups whether an object is in a certain field.
    Currently assumes a single instrument, especially for FoV width and height.
    """
    __nature__ = "SurveyPlan"

    PROPERTIES         = ["pointings", "width", "height"]
    SIDE_PROPERTIES    = ["fields", "ccds"]
    DERIVED_PROPERTIES = []

    def __init__(self, time=None, ra=None, dec=None, band=None, skynoise=None, 
                 obs_field=None, zp=None, comment=None, width=7.295, height=7.465,
                 fields=None, empty=False, load_opsim=None, **kwargs):
        """
        Parameters:
        ----------
        TBA

        """
        self.__build__()
        if empty:
            return

        self.create(time=time,ra=ra,dec=dec,band=band,skynoise=skynoise,
                    obs_field=obs_field, zp=zp, comment=comment,
                    width=width, height=height, fields=fields,
                    load_opsim=load_opsim, **kwargs)

    def create(self, time=None, ra=None, dec=None, band=None, skynoise=None, 
               obs_field=None, zp=None, comment=None,
               width=7.295, height=7.465, fields=None,
               load_opsim=None, **kwargs):
        """
        """        
        self._properties["width"] = float(width)
        self._properties["height"] = float(height)
        self._side_properties["ccds"] = kwargs.pop('ccds', None)
        
        if fields is not None:
            self.set_fields(**fields)

        if load_opsim is None:
            self.add_observation(time, band, skynoise, ra=ra, dec=dec,
                                 zp=zp, comment=comment, field=obs_field)
        else:
            self.load_opsim(load_opsim, **kwargs)

    # =========================== #
    # = Main Methods            = #
    # =========================== #

    # ---------------------- #
    # - Get Methods        - #
    # ---------------------- #

    # ---------------------- #
    # - Setter Methods     - #
    # ---------------------- #
    def set_fields(self, ra=None, dec=None, ccds=None, **kwargs):
        """
        """
        kwargs["width"] = kwargs.get("width", self.width)
        kwargs["height"] = kwargs.get("height", self.height)

        self._side_properties["fields"] = SurveyFieldBins(ra, dec, ccds=self.ccds,
                                                          **kwargs)

        # This appears not to do anything, there is no methof _update_field_radec
        # if self.cadence is not None and np.any(np.isnan(self.cadence['field'])):
        #     warnings.warning("pointings were already set and will be updated")
        #     self._update_field_radec()

    def add_observation(self, time, band, skynoise, ra=None, dec=None, field=None,
                        zp=None, comment=None):
        """
        """
        if ra is None and dec is None and field is None:
            raise ValueError("Either field or ra and dec must to specified.")
        elif ra is None and dec is None:
            if self.fields is None:
                raise ValueError("Survey fields not defined.")
            else:
                idx = self.fields.field_id_index[field]
                ra = self.fields.ra[idx]
                dec = self.fields.dec[idx]
        elif field is None:
            field = np.array([np.nan for r in ra])

        if zp is None:
            zp = np.array([np.nan for r in ra])
        if comment is None:
            comment = np.array(['' for r in ra])

        new_obs = Table(data=[time, band, zp, skynoise, ra, dec, field, comment],
                        names=['time', 'band', 'zp', 'skynoise',
                               'RA', 'Dec', 'field', 'comment'])

        if self._properties['pointings'] is None:
            self._properties['pointings'] = new_obs
        else:
            self._properties['pointings'] = vstack((self._properties['pointings'], 
                                                  new_obs))

    # ---------------------- #
    # - Load Method        - #
    # ---------------------- #
    def load_opsim(self, filename, survey_table='Summary', field_table='Field',
                   band_dict=None, skybright_key='filtSkyBright', skybright_factor=1.,
                   default_skybright=22.25, zp=30):
        """
        see https://confluence.lsstcorp.org/display/SIM/Summary+Table+Column+Descriptions
        for format description

        Currently only the used columns are loaded

        table_name -- name of table in SQLite DB (deafult "ptf" because of 
                      Eric's example)
        band_dict -- dictionary for converting filter names 
        zp -- zero point for converting sky brightness from mag to flux units
              (should match the zp used in instprop for SimulSurvey)
        """        
        import sqlite3
        connection = sqlite3.connect(filename)

        def _fetch(keys, table):
            loaded = odict()
            for key in keys:
                # This is not safe against injection (but should be OK)
                # TODO: Add function to sanitize input
                cmd = 'SELECT %s from %s;'%(key, table)
                tmp = connection.execute(cmd)
                loaded[key] = np.array([a[0] for a in tmp])

            return loaded

        load_keys = ['expMJD', 'filter', 'fieldRA', 'fieldDec', 'fieldID']
        if skybright_key is not None:
            load_keys.append(skybright_key)

        loaded = _fetch(load_keys, survey_table)
        fields = _fetch(['fieldID', 'fieldRA', 'fieldDec'],
                       field_table)
        
        connection.close()

        loaded['fieldRA'] /= _d2r
        loaded['fieldDec'] /= _d2r

        if skybright_key is not None:
            loaded[skybright_key] = np.array([(d if (d is not None) and (d > 0.)
                                               else default_skybright)
                                              for d in loaded[skybright_key]])
            loaded['skynoise'] = 10 ** (-0.4 * (loaded[skybright_key] - zp)) / skybright_factor
        else:
            loaded['skynoise'] = np.ones(len(loaded['fieldRA']))
            loaded['skynoise'] *= 10 ** (-0.4 * (default_skybright - zp)) / skybright_factor
        loaded['zp'] = [zp for r_ in loaded_['fieldRA']]

        if band_dict is not None:
            loaded['filter'] = [band_dict[band] for band in loaded['filter']]
        else:
            loaded['filter'] = loaded['filter']

        self.add_observation(loaded['expMJD'],loaded['filter'],loaded['skynoise'],
                             ra=loaded['fieldRA'],dec=loaded['fieldDec'],
                             field=loaded['fieldID'])

        self.set_fields(ra=fields['fieldRA'], dec=fields['fieldDec'],
                        field_id=fields['fieldID'], ccds=self.ccds)

    # ================================== #
    # = Observation time determination = #
    # ================================== #
    def get_obs_fields(self, ra, dec, field_id=None,
                       progress_bar=False, notebook=False):
        """
        """
        if (self.fields is not None and 
            not np.all(np.isnan(self.pointings["field"]))):
            tmp = self.fields.coord2field(ra, dec, field_id=field_id,
                                          progress_bar=progress_bar, 
                                          notebook=notebook)
            return tmp['field'], tmp.get('ccd', None)
        else:
            return None, None

    def get_non_field_obs(self, ra, dec, progress_bar=False, notebook=False):
        """
        """
        observed = False
        gen = self.pointings[np.isnan(self.pointings["field"])]

        if progress_bar and len(gen) > 0:
            try:
                print "Finding transients observed in custom pointings"
                gen = get_progressbar(gen, notebook=notebook)
            except ImportError:
                pass
            except IOError:
                pass

        for k, obs in enumerate(gen):
            tmp_f = SurveyField(obs["RA"], obs["Dec"], 
                                self.width, self.height,
                                ccds=self.fields.ccds)
            tmp = tmp_f.coord_in_field(ra, dec)

            # Setup output as dictionaries that can be converted to Tables and
            # sorted later
            if k == 0:
                if type(tmp['field']) is np.bool_:
                    single_coord = True
                    out = np.array([], dtype=int)
                    ccd = np.array([], dtype=int)
                else:
                    out = [np.array([], dtype=int) for r in ra]
                    ccd = [np.array([], dtype=int) for r in ra]

            if single_coord:
                if tmp['field']:
                    observed = True
                    out = np.append(out, [k])
                    ccd = np.append(ccd, [tmp['ccd']])
            else:
                for l in np.where(tmp['field'])[0]:
                    observed = True
                    out[l] = np.append(out[l], [k])
                    ccd[l] = np.append(ccd[l], [tmp['ccd']])

        if observed:
            return out, ccd
        else:
            return None, None

    def observed_on(self, fields=None, ccds=None,
                    non_field=None, non_field_ccds=None,
                    mjd_range=None):
        """
        mjd_range must be 2-tuple
        fields and non_field np.arrays
        """
        if fields is None and non_field is None:
            raise ValueError("Provide arrays of fields and/or other pointings")

        out = {'time': [], 'band': [], 'skynoise': [], 'field': [],
               'zp': [], 'comment': []}

        if ccds is not None:
            out['ccd'] = []

        if fields is not None:
            for k, l in enumerate(fields):
                mask = (self.pointings['field'] == l)
                out['time'].extend(self.pointings['time'][mask].quantity.value)
                out['band'].extend(self.pointings['band'][mask])
                out['zp'].extend(self.pointings['zp'][mask])
                out['comment'].extend(self.pointings['comment'][mask])
                out['skynoise'].extend(self.pointings['skynoise']
                                       [mask].quantity.value)
                out['field'].extend(l*np.ones(np.sum(mask), dtype=int))
                if 'ccd' in out.keys():
                    out['ccd'].extend(ccds[k]*np.ones(np.sum(mask), dtype=int))

        if non_field is not None:
            mask = np.isnan(self.pointings["field"])
            out['time'].extend(self.pointings['time'][mask][non_field].quantity.value)
            out['band'].extend(self.pointings['band'][mask][non_field])
            out['zp'].extend(self.pointings['zp'][mask][non_field])
            out['comment'].extend(self.pointings['comment'][mask][non_field])
            out['skynoise'].extend(self.pointings['skynoise']
                                   [mask][non_field].quantity.value)
            out['field'].extend(np.nan*np.ones(np.sum(mask), dtype=int))
            if 'ccd' in out.keys():
                out['ccd'].extend(non_field_ccds[k][mask])

        table = Table(out, meta={})
        idx = np.argsort(table['time'])
        if mjd_range is None:
            return table[idx]
        else:
            t = table[idx]
            return t[(t['time'] >= mjd_range[0]) &
                     (t['time'] <= mjd_range[1])]

    # =========================== #
    # = Properties and Settings = #
    # =========================== #
    @property
    def pointings(self):
        """Table of observations"""
        return self._properties["pointings"]

    @property
    def cadence(self):
        """Table of observations"""
        warnings.warn("cadence has been renamed pointings", DeprecationWarning)
        return self._properties["pointings"]

    @property
    def width(self):
        """field width"""
        return self._properties["width"]

    @property
    def height(self):
        """field height"""
        return self._properties["height"]

    # ------------------
    # - Side properties
    @property
    def fields(self):
        """Observation fields"""
        return self._side_properties["fields"]

    @property
    def ccds(self):
        """Camera CCDs"""
        return self._side_properties["ccds"]

#######################################
#                                     #
# LigthcurveCollecion object          #
#                                     #
#######################################
class LightcurveCollection( BaseObject ):
    """
    LightcurveCollection
    Collects and organizes lightcurves (e.g. simulated by a Survey object)
    for easy access and serialization while try to avoid excessive memory
    use by Astropy Tables. Superficially acts like a list of tables but
    creates them on the fly from structured numpy arrays
    """
    __nature__ = "LightcurveCollection"

    PROPERTIES         = ['lcs', 'meta', 'meta_rejected']
    SIDE_PROPERTIES    = ['threshold', 'n_samenight', 'p_bins']
    DERIVED_PROPERTIES = ['stats']

    def __init__(self,  threshold=5., n_samenight=2,
                 p_bins=np.arange(-30, 71, 5), empty=False,
                 **kwargs):
        """
        Parameters:
        ----------
        TBA

        """
        self.__build__()
        self.set_threshold(threshold)
        self.set_n_samenight(n_samenight)
        self.set_p_bins(p_bins)
        self._prep_stats_()

        if empty:
            return

        self.create(**kwargs)

    def create(self, lcs=None, load=None):
        """
        """
        if load is None:
            self.add(lcs)
        else:
            self.load(load)

    # =========================== #
    # = Main Methods            = #
    # =========================== #
    def add(self, lcs):
        """
        """
        if type(lcs) is list:
            meta = [lc.meta for lc in lcs]
        else:
            meta = lcs.meta


        mask = self._add_lcs_(lcs)
        self._add_meta_(meta, mask=mask)    

    def load(self, filename):
        """
        """
        loaded = cPickle.load(open(filename))
        self._properties['lcs'] = loaded['lcs']
        self._properties['meta'] = loaded['meta']
        if 'meta_rejected' in loaded.keys():
            self._properties['meta_rejected'] = loaded['meta_rejected']
        if 'stats' in loaded.keys():
            self._derived_properties['stats'] = loaded['stats']
        if 'side' in loaded.keys():
            self._side_properties = loaded['side']

    def save(self, filename):
        """
        """
        cPickle.dump({'lcs': self._properties["lcs"],
                      'meta': self._properties["meta"],
                      'meta_rejected': self._properties["meta_rejected"],
                      'stats': self._derived_properties["stats"],
                      'side': self._side_properties},
                     open(filename, 'w'))

    # ---------------------- #
    # - Get Methods        - #
    # ---------------------- #
    def __getitem__(self, given):
        """
        """
        if isinstance(given, slice):
            return [Table(data=data,
                          meta={k: v for k, v in zip(meta.dtype.names, meta)})
                    for data, meta in
                    zip(self.lcs[given], self.meta[given])]
        else:
            meta = self.meta[given]
            return Table(data=self.lcs[given],
                         meta={k: v for k, v in zip(meta.dtype.names, meta)}) 
            
    # ---------------------- #
    # - Add Methods        - #
    # ---------------------- #

    def _add_lcs_(self, lcs):
        """
        """
        if self.lcs is None:
            self._properties['lcs'] = []

        if type(lcs) is list:
            mask = []
            for lc in lcs:
                mask.append(self._add_lc_(lc))
        else:
            mask = self._add_lc_(lcs)

        return mask

    def _add_lc_(self, lc):
        """
        """
        mask = self._add_lc_stats_(lc)
        if mask:
            self._properties['lcs'].append(lc.as_array())
        return mask

    def _add_meta_(self, meta, mask):
        """
        """
        if type(meta) is list:                
            for (meta_, mask_) in zip(meta, mask):
                if mask_:
                    self._add_meta_info_(meta_)
                else:
                    self._add_meta_info_(meta_, suffix='_rejected')
        else:
            if mask:
                self._add_meta_info_(meta)
            else:
                self._add_meta_info_(meta, suffix='_rejected')

    def _add_meta_info_(self, info, suffix=''):
        """
        """
        meta_name = 'meta%s'%suffix

        if self._properties[meta_name] is None:
            keys = [k for k in info.keys()]
            dtypes = [type(v) for v in info.values()]
            self._create_meta_(keys, dtypes, suffix)

        for k in self._properties[meta_name].keys():
            self._properties[meta_name][k] = np.append(
                self._properties[meta_name][k],
                info[k]
            )

    def _create_meta_(self, keys, dtypes, suffix=''):
        """
        Create the ordered ditcionary of meta parameters based of first item
        """
        meta_name = 'meta%s'%suffix
        self._properties[meta_name] = odict()
        for k, t in zip(keys, dtypes):
            self._properties[meta_name][k] = np.array([], dtype=t)

    def _prep_stats_(self):
        """
        """
        self._derived_properties['stats'] = {
            'p_det': np.array([]), 
            'p_last': np.array([]), 
            'dt_det': np.array([]), 
            'p_binned': {'all': None},
            'mag_max': {}
        }

    def _add_lc_stats_(self, lc):
        """
        """
        p0, p1, dt = get_p_det_last(lc, thr=self.threshold,
                                    n_samenight=self.n_samenight)
        if p0 < 1e11 and p1 > -1e11:
            self._derived_properties['stats']['p_det'] = np.append(
                self._derived_properties['stats']['p_det'], p0
            )
            self._derived_properties['stats']['p_last'] = np.append(
                self._derived_properties['stats']['p_last'], p1
            )
            self._derived_properties['stats']['dt_det'] = np.append(
                self._derived_properties['stats']['dt_det'], dt
            )

            self._add_p_binned_(lc)
            self._add_mag_max_(lc)
            return True
        else:
            return False

    def _add_p_binned_(self, lc):
        """
        """
        new = np.array([np.histogram(lc['time'] - lc.meta['t0'],
                                     bins=self.p_bins)[0]])
        if self.stats['p_binned']['all'] is not None:
            new = np.concatenate(
                (self._derived_properties['stats']['p_binned']['all'], new),
                axis=0
            )    
        self._derived_properties['stats']['p_binned']['all'] = new

        for b_ in np.unique(lc["band"]):
            lc_b = lc[lc["band"] == b_]
            new = np.array([np.histogram(lc_b['time'] - lc_b.meta['t0'],
                                         bins=self.p_bins)[0]])
            if b_ not in self.stats['p_binned'].keys():
                if len(self.stats['p_det']) == 1:
                    self._derived_properties['stats']['p_binned'][b_] = new
                else:
                    tmp = np.zeros(self.stats['p_binned']['all'].shape)
                    tmp[-1] = new
                    self._derived_properties['stats']['p_binned'][b_] = tmp
            else:
                new = np.concatenate(
                    (self._derived_properties['stats']['p_binned'][b_], new),
                    axis=0
                )
                self._derived_properties['stats']['p_binned'][b_] = new

        missing = [b_ for b_ in self.stats['p_binned'].keys()
                   if b_ not in np.unique(lc["band"]) and b_ != 'all']
        for b_ in missing:
            new = np.zeros((1, len(self.p_bins)-1))
            new = np.concatenate(
                (self._derived_properties['stats']['p_binned'][b_], new),
                axis=0
            )
            self._derived_properties['stats']['p_binned'][b_] = new

    def _add_mag_max_(self, lc):
        """
        """
        for b_ in np.unique(lc["band"]):
            mag_max = get_lc_max(lc, b_)
            if b_ not in self.stats['mag_max'].keys():
                new = 99. * np.ones(len(self.stats['p_det']))
                new[-1] = mag_max
                self._derived_properties['stats']['mag_max'][b_] = new
            else:
                new = np.append(
                    self._derived_properties['stats']['mag_max'][b_], mag_max
                )
                self._derived_properties['stats']['mag_max'][b_] = new

        missing = [b_ for b_ in self.stats['mag_max'].keys()
                   if b_ not in np.unique(lc["band"])]
        for b_ in missing:
            new = np.append(
                self._derived_properties['stats']['mag_max'][b_], 99.
            )
            self._derived_properties['stats']['mag_max'][b_] = new

    # =========================== #
    # = Properties and Settings = #
    # =========================== #
    @property
    def lcs(self):
        """List of lcs as numpy structured arrays without meta parameters"""
        return self._properties["lcs"]

    @property
    def meta(self):
        """numpy structured array with of meta parameters"""
        if self._properties["meta"] is None:
            return None
        return dict_to_array(self._properties["meta"])

    @property
    def meta_rejected(self):
        """numpy structured array with of meta parameters
        of transients rejected by the filter
        """
        if self._properties["meta_rejected"] is None:
            return None
        return dict_to_array(self._properties["meta_rejected"])

    @property
    def meta_full(self):
        """numpy structured array with of meta parameters
        of all simulated transients
        """
        if (self._properties["meta"] is not None and
            self._properties["meta_rejected"] is not None):
            out = odict()
            for k in self.meta.dtype.names:
                out[k] = np.concatenate((self.meta[k], self.meta_rejected[k]))
            return dict_to_array(out)
        elif self._properties["meta_rejected"] is None:
            return self.meta
        elif self._properties["meta"] is None:
            return self.meta_rejected
        else:
            return None

    @property
    def threshold(self):
        """"""
        return self._side_properties["threshold"]

    def set_threshold(self, threshold):
        """
        """
        self._side_properties["threshold"] = threshold

    @property
    def n_samenight(self):
        """"""
        return self._side_properties["n_samenight"]

    def set_n_samenight(self, n_samenight):
        """
        """
        self._side_properties["n_samenight"] = n_samenight

    @property
    def p_bins(self):
        """"""
        return self._side_properties["p_bins"]

    def set_p_bins(self, p_bins):
        """
        """
        self._side_properties["p_bins"] = p_bins

    @property
    def stats(self):
        """"""
        return self._derived_properties["stats"]

# ========================= #
# = Lightcurve statistics = #
# ========================= #
def get_p_det_last(lc, thr=5., n_samenight=2):
    """
    """
    mask_det = lc['flux']/lc['fluxerr'] > thr
    idx_nights = identify_nights(lc['time'])

    if np.sum(mask_det) > 1:
        mult_det = [k_ for k_, idx_ in enumerate(idx_nights)
                    if np.sum(mask_det[idx_]) >= n_samenight]
        if len(mult_det) > 0:
            k__ = idx_nights[mult_det[0]][mask_det[idx_nights[mult_det[0]]]]
            p0 = lc['time'][k__][1] - lc.meta['t0']
            if k__[0] > 0:
                dt = lc['time'][k__[0]] - lc['time'][k__[0] - 1]
            else:
                dt = 1e12
        else:
            p0 = 1e12
            dt = 1e12
            
        p1 = lc['time'].max() - lc.meta['t0']
    else:
        p0 = 1e12
        dt = 1e12
        p1 = -1e12
        
    return p0, p1, dt

def identify_nights(t, interval=0.25):
    """
    """
    bins = np.arange(int(min(t)), int(max(t)) + 1.01, interval)
    t_binned, _ = np.histogram(t, bins=bins)

    k = 0
    idx_nights = [[]]
    for n in t_binned:
        if n == 0 and len(idx_nights[-1]) > 0:
            idx_nights.append([])
        else:
            idx_nights[-1].extend(range(k, k + n))
            k += n

    return [np.array(idx_) for idx_ in idx_nights if len(idx_) > 0]

def get_lc_max(lc, band):
    """
    """
    lc_b = lc[lc['band'] == band]
    if len(lc_b) > 0:
        max_flux = np.max(lc_b['flux'])
        zp = lc_b['zp'][lc_b['flux'] == max_flux]
        return -2.5 * np.log10(max_flux) + zp
    else:
        return 99.
