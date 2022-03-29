from llvmlite import ir

from numba.core.datamodel.registry import register_default
from numba.core.extending import register_model, models
from numba.core import types
from numba.cuda.types import Dim3, GridGroup, CUDADispatcher, vector_type_list


@register_model(Dim3)
class Dim3Model(models.StructModel):
    def __init__(self, dmm, fe_type):
        members = [
            ('x', types.int32),
            ('y', types.int32),
            ('z', types.int32)
        ]
        super().__init__(dmm, fe_type, members)


@register_model(GridGroup)
class GridGroupModel(models.PrimitiveModel):
    def __init__(self, dmm, fe_type):
        be_type = ir.IntType(64)
        super().__init__(dmm, fe_type, be_type)


@register_default(types.Float)
class FloatModel(models.PrimitiveModel):
    def __init__(self, dmm, fe_type):
        if fe_type == types.float16:
            be_type = ir.IntType(16)
        elif fe_type == types.float32:
            be_type = ir.FloatType()
        elif fe_type == types.float64:
            be_type = ir.DoubleType()
        else:
            raise NotImplementedError(fe_type)
        super(FloatModel, self).__init__(dmm, fe_type, be_type)


register_model(CUDADispatcher)(models.OpaqueModel)


def register_vector_type_model(vector_type):
    """Register vector type data model.

    Parameters
    ----------
    vector_type: VectorType
        The vector type to register.
    """

    class VectorTypeModel(models.StructModel):
        def __init__(self, dmm, fe_type):
            members = [(
                attr_name,
                vector_type.base_type
            ) for attr_name in vector_type.attr_names]
            super().__init__(dmm, fe_type, members)

    register_model(type(vector_type))(VectorTypeModel)


for vector_type in vector_type_list:
    register_vector_type_model(vector_type)
