import os
import numpy as np
import pandas as pd
from statsmodels.gam.api import GLMGam, BSplines
from .neuroCombat import make_design_matrix, fit_LS_model_and_find_priors, find_parametric_adjustments, adjust_data_final

def harmonizationLearn(data, covars, smooth_terms=[]):
    """
    Wrapper for neuroCombat function that returns the harmonization model.
    
    Arguments
    ---------
    data : a numpy array
        data to harmonize with ComBat, dimensions are N_samples x N_features
    
    covars : a pandas DataFrame 
        contains covariates to control for during harmonization
        all covariates must be encoded numerically (no categorical variables)
        must contain a single column "SITE" with site labels for ComBat
        dimensions are N_samples x (N_covariates + 1)
        
    smooth_terms (Optional) :  a list, default []
        names of columns in covars to include as smooth, nonlinear terms
        can be any or all columns in covars, except "SITE"
        if empty, ComBat is applied with a linear model of covariates
        if not empty, Generalized Additive Models (GAMs) are used
        will increase computation time due to search for optimal smoothing
    
    Returns
    -------
    model : a dictionary of estimated model parameters
        design, s_data, stand_mean, var_pooled, B_hat, grand_mean,
        gamma_star, delta_star, info_dict (a neuroCombat invention)
    
    bayes_data : a numpy array
        harmonized data, dimensions are N_samples x N_features
    
    """
    # transpose data as per ComBat convention
    data = data.T
    # prep covariate data
    batch_col = covars.columns.get_loc('SITE')
    cat_cols = []
    num_cols = [covars.columns.get_loc(c) for c in covars.columns if c!='SITE']
    smooth_cols = [covars.columns.get_loc(c) for c in covars.columns if c in smooth_terms]
    # maintain a dictionary of smoothing information
    smooth_model = {
        'perform_smoothing': len(smooth_terms)>0,
        'smooth_terms': smooth_terms,
        'smooth_cols': smooth_cols,
        'bsplines_constructor': None,
        'smooth_design': None,
        'formula': None,
        'df_gam': None
    }
    covars = np.array(covars, dtype='object')
    ### additional setup code from neuroCombat implementation:
    # convert batch col to integer
    covars[:,batch_col] = np.unique(covars[:,batch_col],return_inverse=True)[-1]
    # create dictionary that stores batch info
    (batch_levels, sample_per_batch) = np.unique(covars[:,batch_col],return_counts=True)
    info_dict = {
        'batch_levels': batch_levels.astype('int'),
        'n_batch': len(batch_levels),
        'n_sample': int(covars.shape[0]),
        'sample_per_batch': sample_per_batch.astype('int'),
        'batch_info': [list(np.where(covars[:,batch_col]==idx)[0]) for idx in batch_levels]
    }
    ###
    design = make_design_matrix(covars, batch_col, cat_cols, num_cols)
    ### additional setup if smoothing is performed
    if smooth_model['perform_smoothing']:
        # create cubic spline basis for smooth terms
        X_spline = covars[:, smooth_cols].astype(float)
        bs = BSplines(X_spline, df=[10] * len(smooth_cols), degree=[3] * len(smooth_cols))
        # construct formula and dataframe required for gam
        formula = 'y ~ '
        df_gam = {}
        for b in batch_levels:
            formula = formula + 'x' + str(b) + ' + '
            df_gam['x' + str(b)] = design[:, b]
        for c in num_cols:
            if c not in smooth_cols:
                formula = formula + 'c' + str(c) + ' + '
                df_gam['c' + str(c)] = covars[:, c].astype(float)
        formula = formula[:-2] + '- 1'
        df_gam = pd.DataFrame(df_gam)
        # for matrix operations, a modified design matrix is required
        design_gam = np.concatenate((df_gam, bs.basis), axis=1)
        # store objects in dictionary
        smooth_model['bsplines_constructor'] = bs
        smooth_model['formula'] = formula
        smooth_model['df_gam'] = df_gam
        smooth_model['smooth_design'] = design_gam
    ###
    # run steps to perform ComBat
    s_data, stand_mean, var_pooled, B_hat, grand_mean = StandardizeAcrossFeatures(
        data, design, info_dict, smooth_model)
    LS_dict = fit_LS_model_and_find_priors(s_data, design, info_dict)
    gamma_star, delta_star = find_parametric_adjustments(s_data, LS_dict, info_dict)
    bayes_data = adjust_data_final(s_data, design, gamma_star, delta_star, stand_mean, var_pooled, info_dict)
    # save model parameters in single object
    model = {'design': design, 's_data': s_data, 'stand_mean': stand_mean, 'var_pooled':var_pooled,
             'B_hat':B_hat, 'grand_mean': grand_mean, 'gamma_star': gamma_star,
             'delta_star': delta_star, 'n_batch': info_dict['n_batch']}
    # transpose data to return to original shape
    bayes_data = bayes_data.T
    return model, bayes_data

def StandardizeAcrossFeatures(X, design, info_dict, smooth_model):
    """
    The original neuroCombat function standardize_across_features plus
    necessary modifications.
    
    This function will return all estimated parameters in addition to the
    standardized data.
    """
    n_batch = info_dict['n_batch']
    n_sample = info_dict['n_sample']
    sample_per_batch = info_dict['sample_per_batch']

    # perform smoothing with GAMs if selected
    if smooth_model['perform_smoothing']:
        smooth_cols = smooth_model['smooth_cols']
        design = smooth_model['smooth_design']
        bs = smooth_model['bsplines_constructor']
        formula = smooth_model['formula']
        df_gam = smooth_model['df_gam']
        
        if X.shape[0] > 10:
            print('\nWARNING: more than 10 variables will be harmonized with smoothing model.')
            print(' Computation will take some time. For linear model (faster) remove arg: smooth_terms.')
        # initialize penalization weight (not the final weight)
        alpha = np.array([1.0] * len(smooth_cols))
        # initialize an empty matrix for beta
        B_hat = np.zeros((design.shape[1], X.shape[0]))
        # estimate beta for each variable to be harmonized
        for i in range(0, X.shape[0]):
            df_gam.loc[:, 'y'] = X[i, :]
            gam_bs = GLMGam.from_formula(formula, data=df_gam, smoother=bs, alpha=alpha)
            res_bs = gam_bs.fit()
            # Optimal penalization weights alpha can be obtained through gcv
            gam_bs.alpha = gam_bs.select_penweight()[0]
            res_bs_optim = gam_bs.fit()
            B_hat[:, i] = res_bs_optim.params
    else:
        B_hat = np.dot(np.dot(np.linalg.inv(np.dot(design.T, design)), design.T), X.T)
    grand_mean = np.dot((sample_per_batch/ float(n_sample)).T, B_hat[:n_batch,:])
    var_pooled = np.dot(((X - np.dot(design, B_hat).T)**2), np.ones((n_sample, 1)) / float(n_sample))

    stand_mean = np.dot(grand_mean.T.reshape((len(grand_mean), 1)), np.ones((1, n_sample)))
    tmp = np.array(design.copy())
    tmp[:,:n_batch] = 0
    stand_mean  += np.dot(tmp, B_hat).T

    s_data = ((X- stand_mean) / np.dot(np.sqrt(var_pooled), np.ones((1, n_sample))))

    return s_data, stand_mean, var_pooled, B_hat, grand_mean

def saveHarmonizationModel(model, fldr_name):
    """
    Save a harmonization model from harmonizationLearn().
    
    For saving model contents, this function will create a new folder specified
    by fldr_name, and store numpy arrays as .npy files.
    
    If smoothing is performed, additional objects are saved.    
    
    """
    #fldr_name = fldr_name.replace('/', '')
    if os.path.exists(fldr_name):
        raise ValueError('Model folder already exists: %s Change name or delete to save.' % fldr_name)
    else:
        os.makedirs(fldr_name)
    # cleanup model object for saving to file
    do_not_save = ['design', 's_data', 'stand_mean', 'n_batch']
    for key in list(model.keys()):
        if key not in do_not_save:
            obj_size = model[key].nbytes / 1e6
            print('Saving model object: %s, size in MB: %4.2f' % (key, obj_size))
            np.save(fldr_name + '/' + key + '.npy', model[key])
    return None