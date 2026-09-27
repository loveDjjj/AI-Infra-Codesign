'可选 MILP：在主机时间预算内选择实验。调用方提供实测或预测成本与价值，不虚构模拟器性能模型。'
def select_budget(costs,values,budget,time_limit=10):
    if len(costs)!=len(values) or budget<0 or any(cost<=0 for cost in costs):raise ValueError('Positive costs, matching values and nonnegative budget required')
    try:
        from scipy.optimize import milp,Bounds,LinearConstraint
        import numpy as np
    except ImportError:raise RuntimeError('Optional scipy MILP backend is not installed; no solver conclusion was produced') from None
    result=milp(c=-np.asarray(values,dtype=float),integrality=np.ones(len(costs)),bounds=Bounds(0,1),constraints=LinearConstraint([costs],-np.inf,budget),options={'time_limit':time_limit})
    return {'status':int(result.status),'message':result.message,'selected':[index for index,value in enumerate(result.x) if value>.5] if result.x is not None else None,'gap':getattr(result,'mip_gap',None),'optimal':result.status==0}
