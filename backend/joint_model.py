"""Partial-label joint growth curves: one latent velocity/curvature per home.

All three forecast losses train the same coefficient functions. This is not
three independent regressors followed by cosmetic line interpolation.
"""
import os
os.environ.setdefault('OMP_NUM_THREADS','2')
os.environ.setdefault('OPENBLAS_NUM_THREADS','2')
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor

HORIZONS=np.array([3,6,12])


def basis(months,gap=4.):
    h=np.asarray(months,dtype=float)
    # Lagged six-month current medians precede the actual forecast date.
    # The smooth catch-up term also vanishes exactly at the reference origin.
    velocity=(h+gap*(1-np.exp(-h/1.5)))/(12+gap*(1-np.exp(-8)))
    curvature=(h/12)*(h-6)/6
    return np.column_stack([velocity,curvature])


def available_mask(frame,y,available,cutoff):
    cutoff=pd.Period(cutoff,freq='M')
    return np.isfinite(y)&(available<=cutoff.ordinal)&frame.month.lt(str(cutoff)).to_numpy()[:,None]


class JointCurve:
    def __init__(self,spec,features):
        self.spec=dict(spec)
        self.features=list(features)

    def _head(self):
        return HistGradientBoostingRegressor(max_iter=self.spec.get('iterations',150),
            max_leaf_nodes=self.spec.get('leaves',7),l2_regularization=1.,
            learning_rate=.05,min_samples_leaf=30,early_stopping=False,random_state=42)

    def fit(self,frame,y,available,cutoff):
        mask=available_mask(frame,y,available,cutoff)
        keep=mask.any(axis=1)
        x=frame.loc[keep].copy(); yy=np.where(mask[keep],y[keep],0.)
        m=mask[keep]
        self.cutoff_=str(cutoff)
        self.training_rows_=len(x)
        self.training_tasks_={str(h):int(m[:,j].sum()) for j,h in enumerate(HORIZONS)}
        self.last_label_available_=str(pd.Period(ordinal=int(available[keep][m].max()),freq='M'))
        if len(x)<200:
            raise ValueError('Insufficient joint training rows')
        weight=m*np.array([1.,1.,self.spec.get('year_weight',2.)])
        half=self.spec.get('half_life',48)
        if half:
            age=np.maximum(0,pd.Period(cutoff).ordinal-available[keep])
            weight*=np.exp2(-age/half)
        # Each origin month gets equal aggregate influence.
        density=x.groupby('month').month.transform('size').to_numpy()
        rowweight=1/density
        b=basis(HORIZONS,self.spec.get('gap',4.))
        l,c=b[:,0],b[:,1]
        curvature=np.zeros(len(x))
        penalty=self.spec.get('curvature_penalty',.25)
        self.objectives_=[]
        for _ in range(self.spec.get('rounds',2)):
            denom=(weight*l*l).sum(axis=1)
            target=(weight*l*(yy-curvature[:,None]*c)).sum(axis=1)/np.maximum(denom,1e-12)
            w=rowweight*denom
            self.velocity_=self._head().fit(x[self.features],target,sample_weight=w/w.mean())
            velocity=self.velocity_.predict(x[self.features])
            if self.spec.get('constant_growth',False):
                self.curvature_=None
                curvature=np.zeros(len(x))
            else:
                denom=(weight*c*c).sum(axis=1)+penalty*weight.sum(axis=1)
                target=(weight*c*(yy-velocity[:,None]*l)).sum(axis=1)/np.maximum(denom,1e-12)
                w=rowweight*denom
                self.curvature_=self._head().fit(x[self.features],target,sample_weight=w/w.mean())
                curvature=self.curvature_.predict(x[self.features])
            residual=yy-velocity[:,None]*l-curvature[:,None]*c
            self.objectives_.append(float((rowweight[:,None]*weight*residual**2).sum()+(rowweight*weight.sum(axis=1)*penalty*curvature**2).sum()))
        self.bias_=np.zeros(2)
        self.bias_audit_=None
        strength=self.spec.get('recent_short_calibration',0.)
        if strength:
            end=pd.Period(cutoff)-5
            start=end-11
            older=JointCurve({**self.spec,'recent_short_calibration':0.},self.features).fit(frame,y,available,start)
            query=frame.month.between(str(start),str(end)).to_numpy()&np.isfinite(y[:,0])&(available[:,0]<=pd.Period(cutoff).ordinal)
            if query.sum()>=40:
                residual=y[query,0]-older.predict(frame.loc[query])[:,0]
                monthly=pd.Series(residual,index=frame.loc[query,'month']).groupby(level=0).median()
                delta=float(np.clip(monthly.median()/basis([3],self.spec.get('gap',4.))[0,0],-.2,.2))
                self.bias_[0]=strength*delta
                self.bias_audit_={'known_by':str(cutoff),'predictor_cutoff':str(start),'origin_start':str(start),'origin_end':str(end),'residual_n':int(query.sum()),'velocity_adjustment':float(self.bias_[0])}
        return self

    def coefficients(self,frame):
        v=self.velocity_.predict(frame[self.features])
        a=self.curvature_.predict(frame[self.features]) if self.curvature_ is not None else np.zeros(len(frame))
        return np.column_stack([v,a])+self.bias_

    def predict(self,frame,months=HORIZONS):
        return self.coefficients(frame)@basis(months,self.spec.get('gap',4.)).T


CANDIDATES=[
    {'id':'shared_growth_gap0','constant_growth':True,'gap':0.,'half_life':48},
    {'id':'shared_growth_gap4','constant_growth':True,'gap':4.,'half_life':48},
    {'id':'joint_curve_gap0','gap':0.,'curvature_penalty':.25,'half_life':48},
    {'id':'joint_curve_gap4','gap':4.,'curvature_penalty':.25,'half_life':48},
    {'id':'joint_curve_strong_smoothing','gap':4.,'curvature_penalty':1.,'half_life':48},
    {'id':'joint_curve_recent24','gap':4.,'curvature_penalty':.25,'half_life':24},
    {'id':'joint_curve_allhistory','gap':4.,'curvature_penalty':.25,'half_life':0},
    {'id':'joint_curve_small','gap':4.,'curvature_penalty':.5,'half_life':48,'leaves':3},
    {'id':'joint_curve_year4','gap':4.,'curvature_penalty':.5,'half_life':48,'year_weight':4.},
    {'id':'joint_curve_recent12','gap':4.,'curvature_penalty':.5,'half_life':12},
    {'id':'joint_curve_short_cal25','gap':4.,'curvature_penalty':.25,'half_life':0,'recent_short_calibration':.25},
    {'id':'joint_curve_short_cal50','gap':4.,'curvature_penalty':.25,'half_life':0,'recent_short_calibration':.5},
    {'id':'joint_curve_short_cal100','gap':4.,'curvature_penalty':.25,'half_life':0,'recent_short_calibration':1.},
]
