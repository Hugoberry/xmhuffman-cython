import sys
from setuptools import setup, Extension
from Cython.Build import cythonize

is_macos = sys.platform == "darwin"
is_windows = sys.platform == "win32"
is_linux = not is_macos and not is_windows

if is_windows:
    extra_compile_args = ["/O2"]
elif is_linux:
    extra_compile_args = ["-O3", "-fPIC"]
else:
    extra_compile_args = ["-O3"]

xmhuffman_module = Extension(
    "xmhuffman",
    sources=["xmhuffman.pyx", "src/xmhuffman_kernel.c"],
    include_dirs=["include"],
    extra_compile_args=extra_compile_args,
)

setup(
    name="xmhuffman",
    version="0.3.0",
    description="Cython bindings for Microsoft xVelocity/Vertipaq canonical-Huffman string decoding (PBIX/Power Pivot)",
    author="Igor Cotruta",
    url="https://github.com/Hugoberry/xmhuffman-cython",
    ext_modules=cythonize(
        [xmhuffman_module],
        compiler_directives={"language_level": "3"},
    ),
    python_requires=">=3.8",
    zip_safe=False,
)
