#!/usr/bin/env python
# encoding: utf-8
"""
bayesfit.py

Created by vallis on 2012-06-14.
Copyright (c) 2012 California Institute of Technology
"""

from __future__ import division
import sys, os, math, random, time, multiprocessing
import numpy as N, scipy.linalg as SL
import emcee
import libstempo as T
import sampleutils
from util import timing, numpy_seterr
import Simplex
from scipy.special import erfinv

# utility functions

def precisiondigits(val,err):
    """Determine useful digits of precision for value with error."""

    # with Sarah's correct logic:
    if val == 0 and err == 0:
        return 1
    elif err == 0:
        return 15
    elif abs(val) < err: # this deals with the case where the magnitude of the value is less than the error
        return 1
    else:
        return int(math.floor(math.log10(abs(val))) - math.floor(math.log10(err))) + 1

def rad2minsec(val,fmt='hms'):
    """Convert radians to h,m,s or d,m,s."""

    if fmt == 'hms':
        hrdegs = val*12/math.pi
    else:
        hrdegs = val*180/math.pi

    hrdeg  = int(hrdegs)
    minute = int(60*(hrdegs - hrdeg))
    second = (hrdegs - hrdeg) * 3600 - minute * 60

    return hrdeg, minute, second


# global variables for ranges, priors, and offsets

def pospx(pardict):
    return 1.0 if pardict['PX'] > 0 else 0

#ranges = {'PX': (0.03,10.0), 'M2': (0.0,30.0)}   # give absolute ranges here (e.g., [0,2*math.pi]); otherwise it will be filled up with relative ranges
#ranges = {'PX': (0.03,2.0), 'SINI': (0.,1.), 'M2': (0.0,30.0), 'OM': (50.71,50.74), 'T0': (51626.170,51626.185), 'A1': (55.3296,55.32975), 'ECC': (0.0007966,0.00079720925691)}
multipliers = {}                         # give relative ranges here (units of lsq stdev, e.g. [-4,4]); otherwise will use the default value
#multipliers = {'F0': 100}

priors  = {'PX': (0.03,3.0),'ECC': (0.0,1.0),'SINI': (0,1.0),'M2': (0.0,30.0),               # give physical priors here
           'log10_efac':    (-1,1),'efac':   (0.1,10.0),
           'log10_equad':   (-2,2),'equad':  (0.01,100),
           'log10_jitter':  (-2,2),'jitter': (0.01,100),
           'log10_Ared': (-16,-10),'gammared':    (0,6)}

#priors = {'ECC': (0.0,1.0),'SINI': (0,1.0),'M2': (0.0,3.0),'PX': (0.03,10.0),'POSPX': pospx,'log10_efac': (-1,1)}

default = {'log10_efac':     0, 'efac':   1.0,
           'log10_equad':  -10, 'equad':    0,
           'log10_jitter': -10, 'jitter':   0,
           'log10_Ared':   -20, 'gammared': 0}  # default tempo2 values for extra parameters

offsets = {}                                    # parameters that should be offset from their best-fit value;
                                                # note that offsets are restored when comparing with priors

# for red-noise 1640 runs
#ranges['PB']  = (175.4606616,175.460662389)
#ranges['ECC'] = (0.000796935975685,0.000797409256747)
# prior and log likelihood for emcee

# ranges for J2317+1439
ranges = {'RAJ': (6.096234141,6.09623415276), 'DECJ': (0.255842204113,0.255842226601), 'F0': (-1.88456536587e-11,1.88456536587e-11), 'F1': (-2.05208844075e-16,-2.04284428097e-16), 'PMRA': (-2.07236158776,0.442080521267), 'PMDEC': (1.13763199666,4.99884819693), 'PB': (2.45933146053,2.45933146508), 'PBDOT': (-5.50429041509e-13,1.31636514207e-11), 'A1': (2.31394585263,2.3139501895), 'TASC': (54000.2547666,54000.2547673), 'EPS1': (-8.27952935959e-07,1.90708018935e-06), 'EPS1DOT': (-3.57866593382e-14,3.08417896899e-14), 'EPS2': (-1.71753802182e-06,1.55313528073e-06), 'EPS2DOT': (-2.8342553965e-14,7.00709623957e-14), 'PX': (0.03,3.0)}

def logP(xs):
    global pulsar, parameters, priors, offsets

    pardict = {par: value for par,value in zip (parameters,xs)}

    prior = 1.0
    for par in priors:
        if hasattr(priors[par],'__call__'):
            prior = prior * priors[par](pardict)
        elif par in pardict:
            p0, p1 = priors[par]
            prior = prior / (p1 - p0) if p0 <= pardict[par] + offsets[par] <= p1 else 0

    return -N.inf if prior == 0 else math.log(prior)

def dot(*args):
    return reduce(N.dot,args)

def redlike(pardict,method='inv'):
    global pulsar, err, redF, redf, jitterU

    efac, equad, Ared, jitter = 1.0, 0.0, 0.0, 0.0

    if 'efac' in pardict:
        efac = pardict['efac']
    elif 'log10_efac' in pardict:
        efac = 10**pardict['log10_efac']

    if 'equad' in pardict:
        equad = pardict['equad']
    elif 'log10_equad' in pardict:
        equad = 10**pardict['log10_equad']

    if 'Ared' in pardict:
        Ared = pardict['Ared'] 
    elif 'log10_Ared' in pardict:
        Ared = 10**pardict['log10_Ared']

    if 'jitter' in pardict:
        jitter = pardict['jitter']
    elif 'log10_jitter' in pardict:
        jitter = 10**pardict['log10_jitter']

    if method == 'inv':
        M = pulsar.designmatrix()
        res = N.array(pulsar.residuals(updatebats=False),'d')

        Cdiag = (efac*err)**2 + (equad*1e-6*N.ones(len(err)))**2

        if Ared:
            phi = Ared**2 * redf**(-pardict['gammared'])

        if jitter:
            phi_j = (1e-6*jitter)**2 * N.ones(jitterU.shape[1])

            F   = N.hstack((redF,jitterU)) if Ared else jitterU
            phi = N.hstack((phi,phi_j))    if Ared else phi_j

        if Ared or jitter:
            Ninv = N.diag(1/Cdiag)
            NinvF = dot(Ninv,F)
            X = N.diag(1/phi) + dot(F.T,NinvF)  # invphi + FTNinvF

            Cinv = Ninv - dot(NinvF,N.linalg.inv(X),NinvF.T)
            Cdet = N.sum(N.log(Cdiag)) + N.sum(N.log(phi)) + N.linalg.slogdet(X)[1]
        else:
            Cinv = N.diag(1/Cdiag)
            Cdet = N.sum(N.log(Cdiag))

        CinvM = N.dot(Cinv,M)
        A = dot(M.T,CinvM)

        invA = N.linalg.inv(A)
        CinvMres = dot(res,CinvM)

        res = (- 0.5 * dot(res,Cinv,res) + 0.5 * dot(CinvMres,invA,CinvMres.T) - 0.5 * Cdet - 0.5 * N.linalg.slogdet(A)[1]
               - 0.5 * (M.shape[0] - M.shape[1]) * math.log(2.0*math.pi))
    elif method == 'invold':
        M = pulsar.designmatrix()
        resid = N.array(pulsar.residuals(updatebats=False),'d')

        Cdiag = (efac*err)**2 + (equad*1e-6*N.ones(len(err)))**2

        if Ared:
            C = N.diag(Cdiag) + Ared**2 * N.dot(redF,N.dot(N.diag(redf**(-pardict['gammared'])),redF.T))
            Cinv = N.linalg.inv(C)
        else:
            Cinv = N.diag(1/Cdiag)

        CinvM = N.dot(Cinv,M)
        A = dot(M.T,CinvM)

        Cp = Cinv - dot(CinvM,N.linalg.inv(A),CinvM.T)

        res = (- 0.5 * dot(resid,Cp,resid) + 0.5 * N.linalg.slogdet(Cinv)[1] - 0.5 * N.linalg.slogdet(A)[1]
               - 0.5 * (M.shape[1] - M.shape[0]) * math.log(2.0*math.pi))
    elif method == 'svd':
        M = pulsar.designmatrix()
        res = N.array(pulsar.residuals(updatebats=False),'d')

        U, s, Vh = SL.svd(M)
        G = U[:,M.shape[1]:].copy()

        C = N.dot(G.T,N.dot(N.diag((efac*err)**2),G))
        resid = N.dot(G.T,res)

        cf = SL.cho_factor(C)

        # remember that this is not normalized the same as the other... a factor is missing
        res = -0.5 * N.dot(resid,SL.cho_solve(cf,resid)) - 0.5 * len(resid) * math.log((2*math.pi)) - 0.5 * N.sum(N.log(N.diag(cf[0])**2))
    else:
        raise NotImplementedError

    return res

def logL(xs):
    global pulsar, parameters, offsets, err

    efac = 1.0
    for x,par in zip(xs,parameters):
        if par in pulsar.allpars:
            pulsar[par].val = x + offsets[par]
        elif par == 'efac':
            efac = x + offsets[par]
        elif par == 'log10_efac':
            efac = 10**(x + offsets[par])

    if pulsar.ndim > 0:
        pardict = {par: value for par,value in zip (parameters,xs)}

        return redlike(pardict)
    else:
        if 'equad' in parameters or 'log10_equad' in parameters:
            raise NotImplementedError

        res = pulsar.residuals()
        return -0.5 * N.sum((res * res) / (efac**2 * err * err)) - 0.5 * len(res) * math.log(efac)

def rmsres(xs):
    global pulsar, parameters, offsets, err

    # note that rms residual does not depend on EFAC

    # move the MC-search parameters to the desired spot... (note the offsets)
    for x,par in zip(xs,parameters):
        if par in pulsar.allpars:
            pulsar[par].val = x + offsets[par]

    # if we're marginalizing over some of the parameters, run a tempo2 fit to get the ML
    if pulsar.ndim > 0:
        pulsar.fit()

    res = pulsar.residuals()

    rsum   = N.sum(res / (err * err))
    rsumsq = N.sum((res * res) / (err * err))
    wsum   = N.sum(1.0 / (err * err))

    # following the tempo2 definition:
    #
    # wgt_i = 1.0 / err_i**2 [if fitMode == 1, else 1]
    # sumwt = sum_i wgt_i
    # sum   = sum_i wgt_i * res_i
    # sumsq = sum_i wgt_i * (res_i)**2
    # rms = (sumsq - sum*sum/sumwt)/sumwt
    #
    return math.sqrt((rsumsq - (rsum * rsum)/wsum) / wsum)

# prior and log likelihood for multinest

def logPL(xs):
    logprior = logP(xs)

    return logprior + logL(xs) if logprior > -N.inf else -N.inf

# prior transformation and log likelihood for multinest

mirror_sini = False

def multiprior(cube,ndim,nparams):
    global parameters, ranges, DMdist

    for i,par in enumerate(parameters):
        x0,x1 = ranges[par]
        if parameters[i] == 'PX':
            normalize = 2./(math.erf((DMdist*x1-1.)/(math.sqrt(2)*0.2*DMdist*x1)) - math.erf((DMdist*x0-1.)/(math.sqrt(2)*0.2*DMdist*x0)))
            cube[i] = 1/(DMdist-math.sqrt(2)*0.2*DMdist*erfinv(2*cube[i]/normalize + math.erf((DMdist*x0-1)/(math.sqrt(2)*0.2*DMdist*x0))))
            # cube[i] = x0*math.exp(cube[i]*math.log(x1/x0))                    # logarithmic prior
        elif parameters[i] == 'SINI':
            y0, y1 = math.sqrt(1.0 - x1**2), math.sqrt(1.0 - x0**2)     # find the corresponding cosines
            if mirror_sini:
                y = -1.0 + 2.0 * cube[i]                                # mirrored cosi mapping ([-1,1] -> [0,1] + [1,0]) to avoid abrupt cutoff at sini=1
            else:
                y = y0 + cube[i] * (y1 - y0)                            # draw uniformly in cos
            cube[i] = math.sqrt(1.0 - y**2)                             # compute the sine
            #cube[i] = x0 + cube[i] * (x1 - x0)                                        # uniform prior over range [x0, x1]
        else:
            cube[i] = x0 + cube[i] * (x1 - x0)                          # uniform prior over range [x0, x1]

# evals = 1
# lapse = 0.0

def multilog(cube,ndim,nparams):
    global pulsar, parameters, priors, offsets, err, ranges
    # evals, lapse

    t0 = time.time()

    pardict = {par: value for par,value in zip (parameters,cube)}

    prior = 1.0
    for par in priors:
        if hasattr(priors[par],'__call__'):
            prior = prior * priors[par](pardict)
        elif par in pardict:
            x0, x1 = ranges[par]
            p0, p1 = priors[par]

            if p0 <= pardict[par] + offsets[par] <= p1:
                if par == 'SINI':
                    x0, x1 = math.sqrt(1.0 - x1**2), math.sqrt(1.0 - x0**2)
                    p0, p1 = math.sqrt(1.0 - p1**2), math.sqrt(1.0 - p0**2)

                prior = prior * float((x1 - x0) / (p1 - p0))
            else:
                prior = 0

    efac = 1.0
    for i,par in enumerate(parameters):
        # TO DO: may be slow!
        if par in pulsar.allpars:
            pulsar[par].val = cube[i] + offsets[par]
        elif par == 'efac':
            efac = cube[i] + offsets[par]
        elif par == 'log10_efac':
            efac = 10**(cube[i] + offsets[par])

    if pulsar.ndim > 0:
        # TO DO! note that offsets are not passed to redlike
        res =  (math.log(prior) + redlike(pardict)) if prior > 0 else -N.inf
    else:
        if 'equad' in parameters or 'log10_equad' in parameters:
            raise NotImplementedError

        res = pulsar.residuals()
        res = (math.log(prior) - 0.5 * N.sum((res * res) / (efac**2 * err * err)) - 0.5 * len(res) * math.log(efac)) if prior > 0 else -N.inf

    # lapse = lapse + (time.time() - t0)
    # evals = evals + 1

    # if evals % 100 == 0:
    #     print "Reporting: {0} likes, {1} s/like".format(evals,lapse/evals)

    return res

def randomtuple():
    global parameters, ranges

    while True:
        value = [ranges[par][0] + (ranges[par][1] - ranges[par][0]) * random.normalvariate(0,1) for par in parameters]

        if logP(value) > -N.inf:
            return value

def setuprednoise(components=10):
    global pulsar, redF, redf

    day = 24 * 3600
    year = 365.25 * day

    t = pulsar.toas()
    minx, maxx = N.min(t), N.max(t)
    x = (t - minx) / (maxx - minx)
    T = (day/year) * (maxx - minx)

    size = 2*components
    redF = N.zeros((pulsar.nobs,size),'d')
    redf = N.zeros(size,'d')

    for i in range(components):
        redF[:,2*i]   = N.cos(2*math.pi*(i+1)*x)
        redF[:,2*i+1] = N.sin(2*math.pi*(i+1)*x)

        redf[2*i] = redf[2*i+1] = (i+1) / T

    # include the normalization of the prior in the Fourier matrices
    norm = year**2 / (12 * math.pi**2 * T)
    redF = math.sqrt(norm) * redF

def setupjitter(dt=1):
    global pulsar, jitterU

    times = 86400.0 * pulsar.toas()
    isort = N.argsort(times)
    
    bucket_ref = [times[isort[0]]]
    bucket_ind = [[isort[0]]]
    
    for i in isort[1:]:
        if times[i] - bucket_ref[-1] < dt:
            bucket_ind[-1].append(i)
        else:
            bucket_ref.append(times[i])
            bucket_ind.append([i])
        
    jitterU = N.zeros((len(times),len(bucket_ind)),'d')
    for i,l in enumerate(bucket_ind):
        jitterU[l,i] = 1

# main sampling function

def sample(pulsarfile='cJ0437-4715',pulsardir='.',suffix=None,outputdir='.',
           procs=1,fitpars=None,walkers=200,nsteps=100,ball=None,
           reseed=None,resume=False,useprefitvals=False,showml=False,improveml=False,efficiency='0.8',
           method='emcee',ntemps=1,writeparfile=False,dist=10.):
    global pulsar, multiplier, parameters, ranges, multipliers, priors, offsets, err, DMdist
    # evals, lapse

    DMdist = dist

    if method == 'multinest':
        from mpi4py import MPI
        import pymultinest

        printdebug = MPI.COMM_WORLD.Get_rank() == 0
    else:
        printdebug = True


    # find tempo2 files
    pulsarfile, parfile, timfile = sampleutils.findtempo2(pulsarfile,pulsardir=pulsardir,debug=printdebug)
#    parfile, timfile = '../eptadata/par/' + pulsarfile + '_EPTA_0.0.par', '../eptadata/tim/' + pulsarfile + '_EPTA_0.0.tim'
#    parfile, timfile = '../nanograv/par/' + pulsarfile + '_noPX.par', '../nanograv/tim/' + pulsarfile + '_NANOGrav_dfg+12.tim'
    whichpulsar = os.path.basename(pulsarfile)

    # initialize Cython proxy for tempo2 pulsar
    pulsar = T.tempopulsar(parfile,timfile)

    err = 1e-6 * pulsar.toaerrs

    # print "TOA errors: min {0:.2g} s, avg {1:.2g}, median {2:.2g}, max {3:.2g}".format(N.min(err),N.mean(err),N.median(err),N.max(err))

    # -- set up global lists/dicts of parameter names, offsets, ranges, priors

    # fitting parameters
    if fitpars:
        if fitpars[0] == '+':
            parameters = list(pulsar.pars) + fitpars[1:].split(',')
        else:
            parameters = fitpars.split(',')
    else:
        parameters = pulsar.pars

    if 'log10_Ared' in parameters or 'Ared' in parameters:
        setuprednoise()
    if 'log10_jitter' in parameters or 'jitter' in parameters:
        setupjitter()

    ndim = len(parameters)

    if printdebug:
        print "Fitting {0}/{1} parameters: {2}".format(ndim,pulsar.ndim,' '.join(parameters))

    meta = N.fromiter(((par,pulsar[par].val,pulsar[par].err,pulsar.prefit[par].val,pulsar.prefit[par].err)
                       if par in pulsar.allpars else (par,default[par],0.0,default[par],0.0)
                       for par in parameters),
                      dtype=[('name','a32'),('val','f16'),('err','f16'),('pval','f16'),('perr','f16')])

    # do it here, otherwise it will set the post-fit errors to zero
    for par in parameters:
        if par in pulsar.allpars:
            pulsar[par].fit = False

    if printdebug:
        print "Integrating over {0} parameters: {1}".format(pulsar.ndim,' '.join(pulsar.pars))

    if ball is None:
        ball = 1 if method == 'emcee' else 4

    for par in parameters:
        # start from best-fit and (1-sigma) least-squares error
        if par not in pulsar.allpars:
            center, error = N.longdouble(0), N.longdouble(0)
        elif useprefitvals:
            center, error = pulsar.prefit[par].val, pulsar.prefit[par].err
            if error == 0.0:
                error = pulsar[par].err
                if printdebug:
                    print "Warning: prefit error is zero for parameter {0}! Using post-fit error...".format(par)
        else:
            center, error = pulsar[par].val, pulsar[par].err

        if error == 0.0 and printdebug:
            print "Warning: error is zero for parameters {0}! (May be reset to prior.)".format(par)

        # offset parameters (currently F0 only) so that we handle them with sufficient precision
        offsets[par] = center if par in ['F0'] else 0.0

        # if an absolute range is not prescribed, derive it from the tempo2 best-fit and errors,
        # extending the latter by a prescribed or standard multiplier
        if par not in ranges:
            multiplier = multipliers[par] if par in multipliers else ball
            ranges[par] = ((center - offsets[par]) - multiplier*error, (center - offsets[par]) + multiplier*error)

        # make sure that ranges are compatible with prior ranges
        if par in priors and not hasattr(priors[par],'__call__'):
            offprior = priors[par][0] - offsets[par], priors[par][1] - offsets[par]

            if ranges[par][0] >= offprior[1] or ranges[par][1] <= offprior[0] or ranges[par][1] - ranges[par][0] == 0.0:
                # if the range is fully outside the prior, reset range to prior
                ranges[par] = offprior
            else:
                # otherwise, reset range to intersection of range and prior
                ranges[par] = max(ranges[par][0],offprior[0]), min(ranges[par][1],offprior[1])

        if printdebug:
            print "{0} range: [{1},{2}] + {3}".format(par,ranges[par][0],ranges[par][1],offsets[par])

    # -- main sampling setup and loop

    if method == 'emcee':
        # -- set up

        if reseed:
            # restart from the last step (do we double-count it then?)
            if ntemps > 1:
                data = N.load('{0}/chain-pt-{1}.npy'.format(outputdir,reseed))
                p0 = data[:,:,-1,:]
            else:
                data = N.load('{0}/chain-{1}.npy'.format(outputdir,reseed))
                p0 = [data[:,-1,:]]
        else:
            # initialize walkers in a Gaussian ball (rescaled by ranges)
            p0 = [[randomtuple() for i in range(walkers)] for j in range(ntemps)]

        p1 = [[randomtuple() for i in range(walkers)] for j in range(ntemps)]

        if ntemps > 1:
            sampler = emcee.PTSampler(ntemps,walkers,ndim,logL,logP,threads=int(procs))
        else:
            p0 = p0[0]  # only one temperature
            sampler = emcee.EnsembleSampler(walkers,ndim,logPL,threads=int(procs))

        # -- run!

        with timing("{0} x {1} (x {2} T) samples".format(nsteps,walkers,ntemps)):
            sampler.run_mcmc(p0,nsteps)

        print "Mean acceptance fraction:", N.mean(sampler.acceptance_fraction)

        # -- save everything

        filename = '{0}{1}-{2}.npy'.format(whichpulsar,'' if suffix is None else '-' + suffix,ndim)

        print
        print "Writing to files {0}/*-{1}".format(outputdir,filename)

        N.save('{0}/meta-{1}'.format(outputdir,filename),meta)

        if ntemps > 1:
            N.save('{0}/chain-pt-{1}'.format(outputdir,filename) ,sampler.chain)
            N.save('{0}/lnprob-pt-{1}'.format(outputdir,filename),sampler.lnprobability)

            N.save('{0}/chain-{1}'.format(outputdir,filename) ,sampler.chain[0,:,:,:])
            N.save('{0}/lnprob-{1}'.format(outputdir,filename),sampler.lnprobability[0,:,:])

            allpops, lnprobs = sampler.chain[0,:,-1,:], sampler.lnprobability[0,:,-1]

            lnZ, dlnZ = sampler.thermodynamic_integration_log_evidence(fburnin=0.1)
            print "Global (log) Evidence: %e +/- %e" % (lnZ, dlnZ)
        else:
            N.save('{0}/chain-{1}'.format(outputdir,filename) ,sampler.chain)
            N.save('{0}/lnprob-{1}'.format(outputdir,filename),sampler.lnprobability)

            allpops, lnprobs = sampler.chain[:,-1,:], sampler.lnprobability[:,-1]

        best = N.argmax(lnprobs)
        val_mode, logp_mode = allpops[best,:], lnprobs[best]
        # -- done
    elif method == 'multinest':
        outfile = '{0}/{1}{2}-'.format(outputdir,whichpulsar,'' if suffix is None else '-' + suffix)

        if efficiency[-1] == 'C' or efficiency[-1] == 'c':
            const_eff = True
            eff = float(efficiency[:-1])
        else:
            const_eff = False
            eff = float(efficiency)

        pymultinest.run(multilog,multiprior,ndim,
                        n_live_points=walkers,sampling_efficiency=eff,                            # 0.3/0.8 for evidence/parameter evaluation
                        #importance_nested_sampling = const_eff,const_efficiency_mode = const_eff, # possible with newer MultiNest
                        outputfiles_basename=outfile,resume=resume,verbose=True,init_MPI=False)   # if init_MPI=False, I should be able to use MPI in Python

        # if we're not root, we exit, and let him (her?) do the statistics
        if MPI.COMM_WORLD.Get_rank() != 0:
            sys.exit(0)

        print " Writing to files {0}*".format(outfile)
        print

        for line in open('{0}stats.dat'.format(outfile),'r'):
            if "Global Evidence" in line:
                print line.strip('\n')
        print

        # save tempo2 fit information
        N.save('{0}meta.npy'.format(outfile),meta)

        # now let's have a look at the populations
        cloud = N.loadtxt('{0}post_equal_weights.dat'.format(outfile))

        allpops = cloud[:,:-1]
        lnprobs = cloud[:,-1]

        live = N.loadtxt('{0}phys_live.points'.format(outfile))

        best = N.argmax(live[:,-2])
        val_mode, logp_mode = live[best,:-2], live[best,-2]
    else:
        raise NotImplementedError, ("Unknown sampling method: " + method)

    # further optimize the mode

    if improveml:
        optimizer = Simplex.Simplex(lambda xs: -logPL(xs),val_mode,0.1*N.var(allpops[:,:],axis=0))
        print "Optimizing MAP..."
        minimum, error, iters = optimizer.minimize(maxiters=1000,monitor=1); print
        val_mode = N.array(minimum)

    # statistical analysis

    # print header
    maxlen = max(3,max(map(len,parameters)))

    print '-' * (101 + maxlen + 3)
    print "%*s | tempo2 fit parameters              | mcmc-fit parameters                | diff     | erat     bias" % (maxlen,'par')

    # loop over fitted parameters
    for i,par in enumerate(parameters):
        if useprefitvals:
            val_tempo, err_tempo = meta[i]['pval'], meta[i]['perr']
        else:
            val_tempo, err_tempo = meta[i]['val'], meta[i]['err']

        val_mcmc = (val_mode[i] if showml else N.mean(allpops[:,i])) + offsets[par]   # MCMC values/errors
        err_mcmc = math.sqrt(N.var(allpops[:,i]))                                     # use cond. var. also for ML est.

        if writeparfile and par in pulsar.allpars:
            pulsar[par].val = val_mcmc
            pulsar[par].err = err_mcmc

        try:
            with numpy_seterr(divide='ignore'):
                print ('%*s | %+24.*e ± %.1e | %+24.*e ± %.1e | %+.1e | %.1e %+.1e'
                       % (maxlen,par,                                                   # parameter name
                          precisiondigits(val_tempo,err_tempo),val_tempo,err_tempo,     # tempo2 value and error
                          precisiondigits(val_mcmc, err_mcmc ),val_mcmc, err_mcmc,      # MCMC value and error
                          val_mcmc - val_tempo,                                         # MCMC/tempo2 difference
                          err_mcmc/err_tempo,                                           # ratio of errors
                          (val_mcmc - val_tempo)/err_tempo))                            # difference in units of tempo2 error
        except:
            print "Problem with values:", par, val_tempo, err_tempo, val_mcmc, err_mcmc

    print '-' * (101 + maxlen + 3)

    if writeparfile:
        parfilename = '{0}/{1}{2}-mcmc.par'.format(outputdir,whichpulsar,'' if suffix is None else '-' + suffix)
        pulsar.savepar(parfilename)
        print "Wrote new parfile to", parfilename

    val_tempo2 = [(par['pval'] if useprefitvals else par['val']) - offsets[par['name']] for par in meta]

    dof = pulsar.nobs - pulsar.ndim
    pmchisq = -2.0 * logL(val_mode) / dof
    try:
        pfchisq = -2.0 * logL(val_tempo2) / dof
    except:
        pfchisq = 'NaN'

    print
    print "{0}-fit log L: {1}; post-mcmc (best fit) log L: {2}".format('Pre' if useprefitvals else 'Post',pfchisq,pmchisq)

    pmrms = rmsres(val_mode)
    pfrms = rmsres(val_tempo2)

    print "{0}-fit rms res.: {1}; post-mcmc rms res.: {2}".format('Pre' if useprefitvals else 'Post',pfrms,pmrms)


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='Investigate timing solution with emcee and multinest.')

    parser.add_argument('-d',default='.',             help='directory for par and tim files')
    parser.add_argument('-o',default=None,            help='output directory')
    parser.add_argument('-s',default=None,            help='suffix for save files')
    parser.add_argument('-r',default=None,            help='restart emcee from this chain file')
    parser.add_argument('-f',action='store_true',     help='write best-fit parameter values to .par file [false]')
    parser.add_argument('-R',action='store_true',     help='resume multinest run [false]')

    parser.add_argument('-M',action='store_true',     help='use multinest (use emcee by default)')
    parser.add_argument('-T',type=int,default=1,      help='number of emcee parallel-tempering chains [1 for no PT]')
    parser.add_argument('-E',type=str,default='0.8',  help='multinest efficiency mode [e.g. 0.3, 0.8, append C for constant]')

    parser.add_argument('-p',type=int,default=1,      help='number of processors [1 for emcee, use MPI for multinest]')
    parser.add_argument('-n',default=None,            help='comma-separated list of search parameters [defaults to par-file selection]')
    parser.add_argument('-w',type=int,default=200,    help='number of walkers/live points [200]')
    parser.add_argument('-N',type=int,default=100,    help='number of steps [emcee only, 100]')

    parser.add_argument('-b',type=float,default=None,  help='default range multiplier [1 for emcee, 4 for multinest]')
    parser.add_argument('--prefit',action='store_true',help='use pre-fit origin and errors? [false]')
    parser.add_argument('-P',default=None,             help='set prior (given as var1:p0,p1;var2:p0,p1, etc.) [default, None]')
    parser.add_argument('-L',action='store_true',      help='show ML estimator instead of conditional mean [false]')
    parser.add_argument('-I',action='store_true',      help='improve the ML estimator with an amoeba search [false]')

    parser.add_argument('-D',type=float,default=10.,    help='DM distance (in kpc), used to define prior for PX [default 10 kpc]')

    parser.add_argument('pulsar',help='pulsar name (no suffix)')

    args = parser.parse_args()

    # standard output directories
    if args.o is None:
        args.o = 'chains' if args.M else '../fits'

    if args.r and args.M:
        parser.error('Cannot restart multinest from emcee chain file.')

    if args.R and not args.M:
        parser.error('Currently I do not support resuming emcee runs.')

    if args.T > 1 and args.M:
        parser.error('Cannot use parallel tempering in multinest.')

    # set user-provided priors
    if args.P:
        specs = args.P.split('/')
        for spec in specs:
            try:
                par, p01 = spec.split(':')
                priors[par] = map(float,p01.split(','))
            except ValueError:
                parser.error('Improper prior specification: %s' % spec)

    # allow individual specification of multipliers
    if args.b and ':' in args.b:
        specs = args.b.split('/')
        for spec in specs:
            try:
                par, p01 = spec.split(':')
                multipliers[par] = float(p01)
            except ValueError:
                parser.error('Improper multiplier specification: %s' % spec)

        args.b=None

    sample(pulsarfile=args.pulsar,pulsardir=args.d,outputdir=args.o,suffix=args.s,reseed=args.r,resume=args.R,writeparfile=args.f,
           method=('multinest' if args.M else 'emcee'),ntemps=args.T,efficiency=args.E,
           procs=args.p,fitpars=args.n,walkers=args.w,nsteps=args.N,
           ball=args.b,useprefitvals=args.P,showml=args.L,improveml=args.I,dist=args.D)
