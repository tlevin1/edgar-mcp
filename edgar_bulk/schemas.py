"""Explicit schemas for SEC Financial Statement Data Sets.

Why not inferSchema=True: inference makes Spark read the file once just to
guess types, then again to load it, and the guesses can be wrong. `adsh`
looks like text but `cik` looks like an int, and a zip code like 02139 would
lose its leading zero as a number. A declared schema is one pass and no
surprises. Columns we don't use are still declared, because a TSV schema is
positional.
"""

from pyspark.sql.types import DecimalType, IntegerType, StringType, StructField, StructType

S, I = StringType(), IntegerType()

# Values are exact decimals in the source ("2254000000.0000"). Decimal keeps them
# exact; a double would introduce float error into a check that compares sums.
VALUE = DecimalType(28, 4)

SUB_COLUMNS = [
    ("adsh", S), ("cik", I), ("name", S), ("sic", I), ("countryba", S), ("stprba", S),
    ("cityba", S), ("zipba", S), ("bas1", S), ("bas2", S), ("baph", S), ("countryma", S),
    ("stprma", S), ("cityma", S), ("zipma", S), ("mas1", S), ("mas2", S), ("countryinc", S),
    ("stprinc", S), ("ein", S), ("former", S), ("changed", S), ("afs", S), ("wksi", I),
    ("fye", S), ("form", S), ("period", S), ("fy", I), ("fp", S), ("filed", S),
    ("accepted", S), ("prevrpt", I), ("detail", I), ("instance", S), ("nciks", I), ("aciks", S),
]

NUM_COLUMNS = [
    ("adsh", S), ("tag", S), ("version", S), ("ddate", S), ("qtrs", I), ("uom", S),
    ("segments", S), ("coreg", S), ("value", VALUE), ("footnote", S),
]

TAG_COLUMNS = [
    ("tag", S), ("version", S), ("custom", I), ("abstract", I), ("datatype", S),
    ("iord", S), ("crdr", S), ("tlabel", S), ("doc", S),
]

SUB_SCHEMA = StructType([StructField(n, t, True) for n, t in SUB_COLUMNS])
NUM_SCHEMA = StructType([StructField(n, t, True) for n, t in NUM_COLUMNS])
TAG_SCHEMA = StructType([StructField(n, t, True) for n, t in TAG_COLUMNS])
