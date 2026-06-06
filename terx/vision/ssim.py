try:
    import cv2
    import numpy as np
    from skimage.metrics import structural_similarity as ssim_metric
    _HAS_DEPS = True
except ImportError:
    _HAS_DEPS = False

def compute_ssim(img1_bytes: bytes, img2_bytes: bytes) -> float:
    """
    Compute Structural Similarity Index (SSIM) between two PNG images.
    Returns a score from -1.0 to 1.0 (1.0 = identical).
    """
    if not _HAS_DEPS:
        raise ImportError("SSIM requires opencv and scikit-image: pip install 'terx[vision]'")
    nparr1 = np.frombuffer(img1_bytes, np.uint8)
    nparr2 = np.frombuffer(img2_bytes, np.uint8)
    
    img1 = cv2.imdecode(nparr1, cv2.IMREAD_GRAYSCALE)
    img2 = cv2.imdecode(nparr2, cv2.IMREAD_GRAYSCALE)
    
    if img1 is None or img2 is None:
        raise ValueError("Invalid image bytes provided.")
        
    if img1.shape != img2.shape:
        # Resize img2 to match img1 for comparison (handles minor viewport changes)
        img2 = cv2.resize(img2, (img1.shape[1], img1.shape[0]))
        
    score = ssim_metric(img1, img2, full=False)
    return float(score)
