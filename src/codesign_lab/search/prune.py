from ..config import bootstrap
def reject(config):
    bootstrap()
    from codesign.challenge.hardware import Hardware
    try:hardware=Hardware.from_dict(config['hardware'])
    except (ValueError,TypeError,KeyError) as exc:return 'invalid hardware: '+str(exc)
    if hardware.area_mm2()>24:return 'proven area > 24 mm2'
    return None
