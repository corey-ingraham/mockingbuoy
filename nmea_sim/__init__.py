"""mockingbuoy engine core.

Hardware-agnostic multi-port NMEA 0183 simulator/generator. This package holds the
generation engine only; it MUST NOT import the web layer, uvicorn, or any GUI toolkit
(strict one-way layering: web -> nmea_sim -> serial/generators/state/config).
"""

__version__ = "0.1.0"
