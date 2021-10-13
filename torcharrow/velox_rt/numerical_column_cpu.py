# Copyright (c) Facebook, Inc. and its affiliates.
import array as ar
import math
import operator
import statistics
from typing import Dict, List, Literal, Optional, Union, cast, Callable

import numpy as np
import torcharrow._torcharrow as velox
import torcharrow.dtypes as dt
from torcharrow.expression import expression
from torcharrow.icolumn import IColumn
from torcharrow.inumerical_column import INumericalColumn
from torcharrow.scope import ColumnFactory
from torcharrow.trace import trace

from .column import ColumnFromVelox
from .typing import get_velox_type

# ------------------------------------------------------------------------------


class NumericalColumnCpu(INumericalColumn, ColumnFromVelox):
    """A Numerical Column"""

    # private
    def __init__(self, scope, device, dtype, data, mask):
        # TODO: Refactor the constructors (remove mask, data should be velox.BaseColumn)
        assert dt.is_boolean_or_numerical(dtype)
        super().__init__(scope, device, dtype)
        self._data = velox.Column(get_velox_type(dtype))
        for m, d in zip(mask.tolist(), data.tolist()):
            if m:
                self._data.append_null()
            else:
                self._data.append(d)
        self._finialized = False

    @staticmethod
    def _full(scope, device, data, dtype=None, mask=None):
        assert isinstance(data, np.ndarray) and data.ndim == 1
        if dtype is None:
            dtype = dt.typeof_np_ndarray(data.dtype)
        else:
            if dtype != dt.typeof_np_dtype(data.dtype):
                # TODO fix nullability
                # raise TypeError(f'type of data {data.dtype} and given type {dtype} must be the same')
                pass
        if not dt.is_boolean_or_numerical(dtype):
            raise TypeError(f"construction of columns of type {dtype} not supported")
        if mask is None:
            mask = NumericalColumnCpu._valid_mask(len(data))
        elif len(data) != len(mask):
            raise ValueError(
                f"data length {len(data)} must be the same as mask length {len(mask)}"
            )
        # TODO check that all non-masked items are legal numbers (i.e not nan)
        return NumericalColumnCpu(scope, device, dtype, data, mask)

    # Any _empty must be followed by a _finalize; no other ops are allowed during this time
    @staticmethod
    def _empty(scope, device, dtype):
        return NumericalColumnCpu(
            scope, device, dtype, ar.array(dtype.arraycode), ar.array("b")
        )

    @staticmethod
    def _fromlist(
        scope, device: str, data: List[Union[int, float, bool]], dtype: dt.DType
    ):
        velox_column = velox.Column(get_velox_type(dtype), data)
        return ColumnFromVelox.from_velox(
            scope,
            device,
            dtype,
            velox_column,
            True,
        )

    def _append_null(self):
        if self._finialized:
            raise AttributeError("It is already finialized.")
        self._data.append_null()

    def _append_value(self, value):
        if self._finialized:
            raise AttributeError("It is already finialized.")
        self._data.append(value)

    def _finalize(self):
        self._finialized = True
        return self

    def _valid_mask(self, ct):
        raise np.full((ct,), False, dtype=np.bool8)

    def __len__(self):
        return len(self._data)

    def null_count(self):
        """Return number of null items"""
        return self._data.get_null_count()

    @trace
    def copy(self):
        return self.scope._FullColumn(self._data.copy(), self.mask.copy())

    def getdata(self, i):
        if i < 0:
            i += len(self._data)
        if self._data.is_null_at(i):
            return self.dtype.default
        else:
            return self._data[i]

    def getmask(self, i):
        if i < 0:
            i += len(self._data)
        return self._data.is_null_at(i)

    # if-then-else ---------------------------------------------------------------

    def ite(self, then_, else_):
        """Vectorized if-then-else"""
        if not dt.is_boolean(self.dtype):
            raise TypeError("condition must be a boolean vector")
        if not isinstance(then_, IColumn):
            then_ = self._Column(then_)
        if not isinstance(else_, IColumn):
            else_ = self._Column(else_)
        lub = dt.common_dtype(then_.dtype, else_.dtype)

        if lub is None or dt.is_void(lub):
            raise TypeError(
                "then and else branches must have compatible types, got {then_.dtype} and {else_.dtype}, respectively"
            )
        if isinstance(then_, NumericalColumnCpu) and isinstance(
            else_, NumericalColumnCpu
        ):
            col = velox.Column(get_velox_type(lub))
            for i in range(len(self)):
                if self.getmask(i):
                    col.append_null()
                else:
                    col.append(
                        then_.getdata(i) if self.getdata(i) else else_.getdata(i)
                    )
            return ColumnFromVelox.from_velox(self.scope, self.device, lub, col, True)

        else:
            # refer back to default handling...
            return super.ite(self, then_, else_)

    # sorting, top-k, unique---------------------------------------------------

    @trace
    @expression
    def sort(
        self,
        columns: Optional[List[str]] = None,
        ascending=True,
        na_position: Literal["last", "first"] = "last",
    ):
        """Sort a column/a dataframe in ascending or descending order"""
        if columns is not None:
            raise TypeError("sort on numerical column can't have 'columns' parameter")
        res = []
        none_count = 0
        for i in range(len(self)):
            if self.getmask(i):
                none_count += 1
            else:
                res.append(self.getdata(i))
        res.sort(reverse=not ascending)

        col = velox.Column(get_velox_type(self.dtype))
        if na_position == "first":
            for i in range(none_count):
                col.append_null()
        for value in res:
            col.append(value)
        if na_position == "last":
            for i in range(none_count):
                col.append_null()

        return ColumnFromVelox.from_velox(
            self.scope, self.device, self.dtype, col, True
        )

    @trace
    @expression
    def nlargest(
        self,
        n=5,
        columns: Optional[List[str]] = None,
        keep: Literal["last", "first"] = "first",
    ):
        """Returns a new data of the *n* largest element."""
        if columns is not None:
            raise TypeError(
                "computing n-largest on numerical column can't have 'columns' parameter"
            )
        return self.sort(columns=None, ascending=False, na_position=keep).head(n)

    @trace
    @expression
    def nsmallest(self, n=5, columns: Optional[List[str]] = None, keep="first"):
        """Returns a new data of the *n* smallest element."""
        if columns is not None:
            raise TypeError(
                "computing n-smallest on numerical column can't have 'columns' parameter"
            )

        return self.sort(columns=None, ascending=True, na_position=keep).head(n)

    @trace
    @expression
    def nunique(self, dropna=True):
        """Returns the number of unique values of the column"""
        result = set()
        for i in range(len(self)):
            if self.getmask(i):
                if not dropna:
                    result.add(None)
            else:
                result.add(self.getdata(i))
        return len(result)

    # operators ---------------------------------------------------------------
    def _checked_binary_op_call(
        self, other: Union[INumericalColumn, int, float, bool], op_name: str
    ) -> INumericalColumn:
        if isinstance(other, INumericalColumn):
            self.scope.check_is_same(other.scope)

        if isinstance(other, NumericalColumnCpu):
            result_col = getattr(self._data, op_name)(other._data)
            result_dtype = result_col.dtype().with_null(
                self.dtype.nullable or other.dtype.nullable
            )
            return ColumnFromVelox.from_velox(
                self.scope, self.device, result_dtype, result_col, True
            )
        else:
            # other is scalar
            assert (
                isinstance(other, int)
                or isinstance(other, float)
                or isinstance(other, bool)
            )
            result_col = getattr(self._data, op_name)(other)
            result_dtype = result_col.dtype().with_null(self.dtype.nullable)
            return ColumnFromVelox.from_velox(
                self.scope, self.device, result_dtype, result_col, True
            )

    def _checked_comparison_op_call(
        self,
        other: Union[INumericalColumn, List[int], List[float], int, float],
        op_name: str,
    ) -> INumericalColumn:
        if isinstance(other, list):
            # Reuse the fromlist construction path
            other = self.scope.Column(other)
        return self._checked_binary_op_call(other, op_name)

    def _checked_arithmetic_op_call(
        self, other: Union[int, float, bool], op_name: str, fallback_py_op: Callable
    ) -> INumericalColumn:
        def should_use_py_impl(
            self, other: Union[INumericalColumn, int, float, bool]
        ) -> bool:
            # Arithmetic operations and bitwise operations are not supported in Velox
            # for boolean type, so let's fall back to Pyhton implementation when both
            # operands are boolean
            # TODO: Support native Velox execution for boolean ops
            if dt.is_boolean(self.dtype):
                if isinstance(other, NumericalColumnCpu) and dt.is_boolean(other.dtype):
                    return True
                # TODO
                # After we match PyTorch semantic to promote boolean type to integer
                # when other is an integer scalar, we should return False for that case
                elif not isinstance(other, NumericalColumnCpu):
                    return True
            return False

        if should_use_py_impl(self, other):
            return self._py_arithmetic_op(other, fallback_py_op)

        return self._checked_binary_op_call(other, op_name)

    @trace
    @expression
    def __add__(self, other: Union[INumericalColumn, int, float]) -> INumericalColumn:
        """Vectorized a + b."""
        return self._checked_arithmetic_op_call(other, "add", operator.add)

    @trace
    @expression
    def __radd__(self, other: Union[int, float]) -> INumericalColumn:
        """Vectorized b + a."""
        return self._checked_arithmetic_op_call(
            other, "radd", IColumn.swap(operator.add)
        )

    @trace
    @expression
    def __sub__(self, other: Union[INumericalColumn, int, float]) -> INumericalColumn:
        """Vectorized a - b."""
        return self._checked_arithmetic_op_call(other, "sub", operator.sub)

    @trace
    @expression
    def __rsub__(self, other: Union[int, float]) -> INumericalColumn:
        """Vectorized b - a."""
        return self._checked_arithmetic_op_call(
            other, "rsub", IColumn.swap(operator.sub)
        )

    @trace
    @expression
    def __mul__(self, other: Union[INumericalColumn, int, float]) -> INumericalColumn:
        """Vectorized a * b."""
        return self._checked_arithmetic_op_call(other, "mul", operator.mul)

    @trace
    @expression
    def __rmul__(self, other: Union[int, float]) -> INumericalColumn:
        """Vectorized b * a."""
        return self._checked_arithmetic_op_call(
            other, "rmul", IColumn.swap(operator.mul)
        )

    @trace
    @expression
    def __floordiv__(self, other):
        """Vectorized a // b."""
        if isinstance(other, INumericalColumn):
            self.scope.check_is_same(other.scope)
        if isinstance(other, NumericalColumnCpu):
            col = velox.Column(get_velox_type(dt.float64))
            assert len(self) == len(other)
            for i in range(len(self)):
                if self.getmask(i) or other.getmask(i):
                    col.append_null()
                else:
                    col.append(self.getdata(i) // other.getdata(i))
            return ColumnFromVelox.from_velox(
                self.scope, self.device, dt.float64, col, True
            )
        else:
            col = velox.Column(get_velox_type(dt.float64))
            for i in range(len(self)):
                if self.getmask(i):
                    col.append_null()
                else:
                    col.append(self.getdata(i) // other)
            return ColumnFromVelox.from_velox(
                self.scope, self.device, dt.float64, col, True
            )

    @trace
    @expression
    def __rfloordiv__(self, other):
        """Vectorized b // a."""
        if isinstance(other, INumericalColumn):
            self.scope.check_is_same(other.scope)
        if isinstance(other, NumericalColumnCpu):
            col = velox.Column(get_velox_type(self.dtype))
            assert len(self) == len(other)
            for i in range(len(self)):
                if self.getmask(i) or other.getmask(i):
                    col.append_null()
                else:
                    col.append(other.getdata(i) // self.getdata(i))
            return ColumnFromVelox.from_velox(
                self.scope, self.device, self.dtype, col, True
            )
        else:
            col = velox.Column(get_velox_type(self.dtype))
            for i in range(len(self)):
                if self.getmask(i):
                    col.append_null()
                else:
                    col.append(other // self.getdata(i))
            return ColumnFromVelox.from_velox(
                self.scope, self.device, self.dtype, col, True
            )

    @trace
    @expression
    def __truediv__(self, other):
        """Vectorized a / b."""
        if isinstance(other, INumericalColumn):
            self.scope.check_is_same(other.scope)
        if isinstance(other, NumericalColumnCpu):
            col = velox.Column(get_velox_type(dt.float64))
            assert len(self) == len(other)
            for i in range(len(self)):
                other_data = other.getdata(i)
                if self.getmask(i) or other.getmask(i):
                    col.append_null()
                elif other_data == 0:
                    col.append_null()
                else:
                    col.append(self.getdata(i) / other_data)
            return ColumnFromVelox.from_velox(
                self.scope, self.device, dt.float64, col, True
            )
        else:
            col = velox.Column(get_velox_type(dt.float64))
            for i in range(len(self)):
                if self.getmask(i):
                    col.append_null()
                elif other == 0:
                    col.append_null()
                else:
                    col.append(self.getdata(i) / other)
            return ColumnFromVelox.from_velox(
                self.scope, self.device, dt.float64, col, True
            )

    @trace
    @expression
    def __rtruediv__(self, other):
        """Vectorized b / a."""
        if isinstance(other, INumericalColumn):
            self.scope.check_is_same(other.scope)
        if isinstance(other, NumericalColumnCpu):
            col = velox.Column(get_velox_type(dt.float64))
            assert len(self) == len(other)
            for i in range(len(self)):
                self_data = self.getdata(i)
                if self.getmask(i) or other.getmask(i):
                    col.append_null()
                elif self_data == 0:
                    col.append_null()
                else:
                    col.append(other.getdata(i) / self_data)
            return ColumnFromVelox.from_velox(
                self.scope, self.device, dt.float64, col, True
            )
        else:
            col = velox.Column(get_velox_type(dt.float64))
            for i in range(len(self)):
                self_data = self.getdata(i)
                if self.getmask(i) or self.getdata(i) == 0:
                    col.append_null()
                elif self_data == 0:
                    col.append_null()
                else:
                    col.append(other / self_data)
            return ColumnFromVelox.from_velox(
                self.scope, self.device, dt.float64, col, True
            )

    @trace
    @expression
    def __mod__(self, other: Union[INumericalColumn, int, float]) -> INumericalColumn:
        """Vectorized a % b."""
        return self._checked_arithmetic_op_call(other, "mod", operator.mod)

    @trace
    @expression
    def __rmod__(self, other: Union[int, float]) -> INumericalColumn:
        """Vectorized b % a."""
        return self._checked_arithmetic_op_call(
            other, "rmod", IColumn.swap(operator.mod)
        )

    @trace
    @expression
    def __pow__(self, other):
        """Vectorized a ** b."""
        if isinstance(other, INumericalColumn):
            self.scope.check_is_same(other.scope)
        if isinstance(other, NumericalColumnCpu):
            col = velox.Column(get_velox_type(self.dtype))
            assert len(self) == len(other)
            for i in range(len(self)):
                if self.getmask(i) or other.getmask(i):
                    col.append_null()
                else:
                    col.append(self.getdata(i) ** other.getdata(i))
            return ColumnFromVelox.from_velox(
                self.scope, self.device, self.dtype, col, True
            )
        else:
            col = velox.Column(get_velox_type(self.dtype))
            for i in range(len(self)):
                if self.getmask(i):
                    col.append_null()
                else:
                    col.append(self.getdata(i) ** other)
            return ColumnFromVelox.from_velox(
                self.scope, self.device, self.dtype, col, True
            )

    @trace
    @expression
    def __rpow__(self, other):
        """Vectorized b ** a."""
        if isinstance(other, INumericalColumn):
            self.scope.check_is_same(other.scope)
        if isinstance(other, NumericalColumnCpu):
            col = velox.Column(get_velox_type(self.dtype))
            assert len(self) == len(other)
            for i in range(len(self)):
                if self.getmask(i) or other.getmask(i):
                    col.append_null()
                else:
                    col.append(other.getdata(i) ** self.getdata(i))
            return ColumnFromVelox.from_velox(
                self.scope, self.device, self.dtype, col, True
            )
        else:
            col = velox.Column(get_velox_type(self.dtype))
            for i in range(len(self)):
                if self.getmask(i):
                    col.append_null()
                else:
                    col.append(other ** self.getdata(i))
            return ColumnFromVelox.from_velox(
                self.scope, self.device, self.dtype, col, True
            )

    @trace
    @expression
    def __eq__(
        self, other: Union[INumericalColumn, List[int], List[float], int, float]
    ):
        """Vectorized a == b."""
        return self._checked_comparison_op_call(other, "eq")

    @trace
    @expression
    def __ne__(
        self, other: Union[INumericalColumn, List[int], List[float], int, float]
    ):
        """Vectorized a != b."""
        return self._checked_comparison_op_call(other, "neq")

    @trace
    @expression
    def __lt__(
        self, other: Union[INumericalColumn, List[int], List[float], int, float]
    ):
        """Vectorized a < b."""
        return self._checked_comparison_op_call(other, "lt")

    @trace
    @expression
    def __gt__(
        self, other: Union[INumericalColumn, List[int], List[float], int, float]
    ):
        """Vectorized a > b."""
        return self._checked_comparison_op_call(other, "gt")

    @trace
    @expression
    def __le__(
        self, other: Union[INumericalColumn, List[int], List[float], int, float]
    ):
        """Vectorized a <= b."""
        return self._checked_comparison_op_call(other, "lte")

    @trace
    @expression
    def __ge__(
        self, other: Union[INumericalColumn, List[int], List[float], int, float]
    ):
        """Vectorized a >= b."""
        return self._checked_comparison_op_call(other, "gte")

    @trace
    @expression
    def __and__(self, other: Union[INumericalColumn, int]) -> INumericalColumn:
        """Vectorized a & b."""
        return self._checked_arithmetic_op_call(other, "bitwise_and", operator.__and__)

    @trace
    @expression
    def __rand__(self, other: Union[int]) -> INumericalColumn:
        """Vectorized b & a."""
        return self._checked_arithmetic_op_call(
            other, "bitwise_rand", IColumn.swap(operator.__and__)
        )

    @trace
    @expression
    def __or__(self, other: Union[INumericalColumn, int]) -> INumericalColumn:
        """Vectorized a | b."""
        return self._checked_arithmetic_op_call(other, "bitwise_or", operator.__or__)

    @trace
    @expression
    def __ror__(self, other: Union[int]) -> INumericalColumn:
        """Vectorized b | a."""
        return self._checked_arithmetic_op_call(
            other, "bitwise_ror", IColumn.swap(operator.__or__)
        )

    @trace
    @expression
    def __xor__(self, other: Union[INumericalColumn, int]) -> INumericalColumn:
        """Vectorized a | b."""
        return self._checked_arithmetic_op_call(other, "bitwise_xor", operator.__xor__)

    @trace
    @expression
    def __rxor__(self, other: Union[int]) -> INumericalColumn:
        """Vectorized b | a."""
        return self._checked_arithmetic_op_call(
            other, "bitwise_rxor", IColumn.swap(operator.__xor__)
        )

    @trace
    @expression
    def __invert__(self):
        """Vectorized: ~a."""
        return ColumnFromVelox.from_velox(
            self.scope, self.device, self.dtype, self._data.invert(), True
        )

    @trace
    @expression
    def __neg__(self):
        """Vectorized: - a."""
        return ColumnFromVelox.from_velox(
            self.scope, self.device, self.dtype, self._data.neg(), True
        )

    @trace
    @expression
    def __pos__(self):
        """Vectorized: + a."""
        return self

    @trace
    @expression
    def isin(self, values, invert=False):
        """Check whether list values are contained in data, or column/dataframe (row/column specific)."""
        # Todo decide on wether mask matters?
        if invert:
            raise NotImplementedError()
        col = velox.Column(get_velox_type(dt.boolean))
        for i in range(len(self)):
            if self.getmask(i):
                col.append(False)
            else:
                col.append(self.getdata(i) in values)
        return ColumnFromVelox.from_velox(
            self.scope, self.device, dt.Boolean(self.dtype.nullable), col, True
        )

    @trace
    @expression
    def abs(self):
        """Absolute value of each element of the series."""
        return ColumnFromVelox.from_velox(
            self.scope, self.device, self.dtype, self._data.abs(), True
        )

    @trace
    @expression
    def ceil(self):
        """Rounds each value upward to the smallest integral"""
        return ColumnFromVelox.from_velox(
            self.scope, self.device, self.dtype, self._data.ceil(), True
        )

    @trace
    @expression
    def floor(self):
        """Rounds each value downward to the largest integral value"""
        return ColumnFromVelox.from_velox(
            self.scope, self.device, self.dtype, self._data.floor(), True
        )

    @trace
    @expression
    def round(self, decimals=0):
        """Round each value in a data to the given number of decimals."""
        # TODO: round(-2.5) returns -2.0 in Numpy/PyTorch but returns -3.0 in Velox
        # return ColumnFromVelox.from_velox(self.scope, self.device, self.dtype, self._data.round(), True)

        col = velox.Column(get_velox_type(self.dtype))
        for i in range(len(self)):
            if self.getmask(i):
                col.append_null()
            else:
                col.append(round(self.getdata(i), decimals))
        return ColumnFromVelox.from_velox(
            self.scope, self.device, self.dtype, col, True
        )

    # data cleaning -----------------------------------------------------------

    @trace
    @expression
    def fillna(self, fill_value: Union[dt.ScalarTypes, Dict]):
        """Fill NA/NaN values using the specified method."""
        if not isinstance(fill_value, IColumn.scalar_types):
            raise TypeError(f"fillna with {type(fill_value)} is not supported")
        if not self.isnullable:
            return self
        else:
            col = velox.Column(get_velox_type(self.dtype))
            for i in range(len(self)):
                if self.getmask(i):
                    if isinstance(fill_value, Dict):
                        raise NotImplementedError()
                    else:
                        col.append(fill_value)
                else:
                    col.append(self.getdata(i))
            return ColumnFromVelox.from_velox(
                self.scope, self.device, self.dtype, col, True
            )

    @trace
    @expression
    def dropna(self, how: Literal["any", "all"] = "any"):
        """Return a column with rows removed where a row has any or all nulls."""
        if not self.isnullable:
            return self
        else:
            col = velox.Column(get_velox_type(self.dtype))
            for i in range(len(self)):
                if self.getmask(i):
                    pass
                else:
                    col.append(self.getdata(i))
            return ColumnFromVelox.from_velox(
                self.scope, self.device, self.dtype, col, True
            )

    @trace
    @expression
    def drop_duplicates(
        self,
        subset: Optional[List[str]] = None,
    ):
        """Remove duplicate values from row/frame"""
        if subset is not None:
            raise TypeError(f"subset parameter for numerical columns not supported")
        seen = set()
        col = velox.Column(get_velox_type(self.dtype))
        for i in range(len(self)):
            if self.getmask(i):
                col.append_null()
            else:
                current = self.getdata(i)
                if current not in seen:
                    col.append(current)
                    seen.add(current)
        return ColumnFromVelox.from_velox(
            self.scope, self.device, self.dtype, col, True
        )

    # universal  ---------------------------------------------------------------

    @trace
    @expression
    def min(self, numeric_only=None, fill_value=None):
        """Return the minimum of the non-null values of the Column."""
        result = None
        for i in range(len(self)):
            if not self.getmask(i):
                value = self.getdata(i)
                if result is None or value < result:
                    result = value
        return result

    @trace
    @expression
    def max(self, fill_value=None):
        """Return the maximum of the non-null values of the column."""
        result = None
        for i in range(len(self)):
            if not self.getmask(i):
                value = self.getdata(i)
                if result is None or value > result:
                    result = value
        return result

    @trace
    @expression
    def all(self):
        """Return whether all non-null elements are True in Column"""
        for i in range(len(self)):
            if not self.getmask(i):
                value = self.getdata(i)
                if value == False:
                    return False
        return True

    @trace
    @expression
    def any(self, skipna=True, boolean_only=None):
        """Return whether any non-null element is True in Column"""
        for i in range(len(self)):
            if not self.getmask(i):
                value = self.getdata(i)
                if value == True:
                    return True
        return False

    @trace
    @expression
    def sum(self):
        # TODO Should be def sum(self, initial=None) but didn't get to work
        """Return sum of all non-null elements in Column (starting with initial)"""
        result = 0
        for i in range(len(self)):
            if not self.getmask(i):
                result += self.getdata(i)
        return result

    @trace
    @expression
    def prod(self):
        """Return produce of the values in the data"""
        result = 1
        for i in range(len(self)):
            if not self.getmask(i):
                result *= self.getdata(i)
        return result

    def _accumulate_column(self, func, *, skipna=True, initial=None):
        it = iter(self)
        # res = self.scope.Column(self.dtype)
        res = []
        total = initial
        rest_is_null = False
        if initial is None:
            try:
                total = next(it)
            except StopIteration:
                raise ValueError(f"cum[min/max] undefined for empty column.")
        if total is None:
            raise ValueError(f"cum[min/max] undefined for columns with row 0 as null.")

        res.append(total)
        for element in it:
            if rest_is_null:
                res.append(None)
                continue
            if element is None:
                if skipna:
                    res.append(None)
                else:
                    res.append(None)
                    rest_is_null = True
            else:
                total = func(total, element)
                res.append(total)
        return self.scope.Column(res, self.dtype)

    @trace
    @expression
    def cummin(self):
        """Return cumulative minimum of the data."""
        return self._accumulate_column(min, skipna=True, initial=None)

    @trace
    @expression
    def cummax(self):
        """Return cumulative maximum of the data."""
        return self._accumulate_column(max, skipna=True, initial=None)

    @trace
    @expression
    def cumsum(self):
        """Return cumulative sum of the data."""
        return self._accumulate_column(operator.add, skipna=True, initial=None)

    @trace
    @expression
    def cumprod(self):
        """Return cumulative product of the data."""
        return self._accumulate_column(operator.mul, skipna=True, initial=None)

    @trace
    @expression
    def mean(self):
        """Return the mean of the values in the series."""
        return statistics.mean(value for value in self if value is not None)

    @trace
    @expression
    def median(self):
        """Return the median of the values in the data."""
        return statistics.median(value for value in self if value is not None)

    # @ trace
    # @ expression
    # def mode(self):
    #     """Return the mode(s) of the data."""
    #     return np.ma.mode(self._ma())

    @trace
    @expression
    def Cpu(self, ddof=1):
        """Return the Cpudev(s) of the data."""
        # ignores nulls
        return np.ma.Cpu(self._ma(), ddof=ddof)

    @trace
    @expression
    def percentiles(self, q, interpolation="midpoint"):
        """Compute the q-th percentile of non-null data."""
        if len(self) == 0 or len(q) == 0:
            return []
        out = []
        s = sorted(self)
        for percent in q:
            k = (len(self) - 1) * (percent / 100)
            f = math.floor(k)
            c = math.ceil(k)
            if f == c:
                out.append(s[int(k)])
                continue
            d0 = s[int(f)] * (c - k)
            d1 = s[int(c)] * (k - f)
            out.append(d0 + d1)
        return out

    # unique and montonic  ----------------------------------------------------

    @trace
    @expression
    def is_unique(self):
        """Return boolean if data values are unique."""
        return self.nunique(dropna=False) == len(self)

    @trace
    @expression
    def is_monotonic_increasing(self):
        """Return boolean if values in the object are monotonic increasing"""
        first = True
        prev = None
        for i in range(len(self)):
            if not self.getmask(i):
                current = self.getdata(i)
                if not first:
                    if prev > current:
                        return False
                else:
                    first = False
                prev = current
        return True

    @trace
    @expression
    def is_monotonic_decreasing(self):
        """Return boolean if values in the object are monotonic decreasing"""
        first = True
        prev = None
        for i in range(len(self)):
            if not self.getmask(i):
                current = self.getdata(i)
                if not first:
                    if prev < current:
                        return False
                else:
                    first = False
                prev = current
        return True

    # interop ----------------------------------------------------------------

    @trace
    def to_pandas(self):
        """Convert self to pandas dataframe"""
        # TODO Add type translation
        # Skipping analyzing 'pandas': found module but no type hints or library stubs
        import pandas as pd  # type: ignore

        return pd.Series(self._ma())

    @trace
    def to_arrow(self):
        """Convert self to pandas dataframe"""
        # TODO Add type translation
        import pyarrow as pa  # type: ignore

        return pa.array(self._ma())


# ------------------------------------------------------------------------------
# registering all numeric and boolean types for the factory...
_primitive_types: List[dt.DType] = [
    dt.Int8(),
    dt.Int16(),
    dt.Int32(),
    dt.Int64(),
    dt.Float32(),
    dt.Float64(),
    dt.Boolean(),
]
for t in _primitive_types:
    ColumnFactory.register((t.typecode + "_empty", "cpu"), NumericalColumnCpu._empty)
    ColumnFactory.register((t.typecode + "_full", "cpu"), NumericalColumnCpu._full)
    ColumnFactory.register(
        (t.typecode + "_fromlist", "cpu"), NumericalColumnCpu._fromlist
    )