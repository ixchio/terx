"""
Visual verification module for TERX using Structural Similarity Index (SSIM).
"""

try:
    from terx.vision.ssim import compute_ssim
    HAS_VISION = True
except ImportError:
    HAS_VISION = False
    compute_ssim = None
