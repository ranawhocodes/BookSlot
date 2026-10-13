import io
import base64
import qrcode
from qrcode.image.pil import PilImage


def generate_qr_code_data_uri(url: str) -> str:
    """
    Generates a high-contrast QR code PNG and returns it as a base64 Data URI.
    Ensures maximum scannability on printed posters and WhatsApp browser redirects.
    """
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=2,
    )
    qr.add_data(url)
    qr.make(fit=True)

    img: PilImage = qr.make_image(fill_color="black", back_color="white")
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    b64_data = base64.b64encode(buffer.getvalue()).decode("utf-8")
    return f"data:image/png;base64,{b64_data}"
