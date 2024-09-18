import imageCompression from 'browser-image-compression';

// HUMAN ASSISTANCE NEEDED
// The compressImage function has a confidence level below 0.8 and may need adjustments for production readiness
export async function compressImage(imageFile: File): Promise<File> {
  const options = {
    maxSizeMB: 1,
    maxWidthOrHeight: 1920,
    useWebWorker: true,
    quality: 0.8
  };

  try {
    const compressedFile = await imageCompression(imageFile, options);
    return compressedFile;
  } catch (error) {
    console.error('Error compressing image:', error);
    throw error;
  }
}

export function getImageDimensions(imageFile: File): Promise<{ width: number; height: number }> {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => {
      resolve({
        width: img.width,
        height: img.height
      });
    };
    img.onerror = reject;
    img.src = URL.createObjectURL(imageFile);
  });
}