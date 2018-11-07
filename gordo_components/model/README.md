## Public components: 
  - `gordo_components.model.get_model`
  - `gordo_components.model.base.GordoBaseModel`

----

`get_model(config)` takes a `dict` containing information to build a given model.  


### Example theoretical use:

```python
from keras.models import Model as KerasModel
from gordo_components.model import get_model

config = {
    'type': 'keras',
}

model = get_model(config)
assert isinstance(model, KerasModel)
```