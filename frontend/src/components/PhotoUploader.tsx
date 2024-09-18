import React, { useState, useCallback } from 'react';
import { useDropzone } from 'react-dropzone';
import { compressImage } from '@/services/imageProcessing';
import { uploadPhoto } from '@/services/api';

// HUMAN ASSISTANCE NEEDED
// The following component may need additional refinement for production readiness.
// Please review and enhance error handling, accessibility, and edge cases.

interface PhotoUploaderProps {
  onPhotosUploaded: (urls: string[]) => void;
}

const PhotoUploader: React.FC<PhotoUploaderProps> = ({ onPhotosUploaded }) => {
  const [photos, setPhotos] = useState<{ file: File; preview: string; uploading: boolean }[]>([]);
  const [uploadProgress, setUploadProgress] = useState<{ [key: string]: number }>({});

  const onDrop = useCallback(async (acceptedFiles: File[]) => {
    const newPhotos = await Promise.all(
      acceptedFiles.map(async (file) => {
        const compressedFile = await compressImage(file);
        return {
          file: compressedFile,
          preview: URL.createObjectURL(compressedFile),
          uploading: false,
        };
      })
    );
    setPhotos((prevPhotos) => [...prevPhotos, ...newPhotos]);
  }, []);

  const { getRootProps, getInputProps, isDragActive } = useDropzone({
    onDrop,
    accept: { 'image/*': [] },
    multiple: true,
  });

  const handleUpload = async () => {
    const uploadedUrls: string[] = [];
    for (let i = 0; i < photos.length; i++) {
      if (!photos[i].uploading) {
        setPhotos((prevPhotos) => {
          const newPhotos = [...prevPhotos];
          newPhotos[i].uploading = true;
          return newPhotos;
        });
        try {
          const url = await uploadPhoto(photos[i].file, (progress) => {
            setUploadProgress((prev) => ({ ...prev, [i]: progress }));
          });
          uploadedUrls.push(url);
        } catch (error) {
          console.error('Error uploading photo:', error);
          // Handle upload error
        }
        setPhotos((prevPhotos) => {
          const newPhotos = [...prevPhotos];
          newPhotos[i].uploading = false;
          return newPhotos;
        });
      }
    }
    onPhotosUploaded(uploadedUrls);
  };

  const handleDelete = (index: number) => {
    setPhotos((prevPhotos) => prevPhotos.filter((_, i) => i !== index));
    setUploadProgress((prev) => {
      const newProgress = { ...prev };
      delete newProgress[index];
      return newProgress;
    });
  };

  const handleReorder = (fromIndex: number, toIndex: number) => {
    setPhotos((prevPhotos) => {
      const newPhotos = [...prevPhotos];
      const [removed] = newPhotos.splice(fromIndex, 1);
      newPhotos.splice(toIndex, 0, removed);
      return newPhotos;
    });
  };

  return (
    <div>
      <div {...getRootProps()} className={`dropzone ${isDragActive ? 'active' : ''}`}>
        <input {...getInputProps()} />
        <p>Drag 'n' drop some photos here, or click to select photos</p>
      </div>
      <div className="photo-previews">
        {photos.map((photo, index) => (
          <div key={photo.preview} className="photo-preview">
            <img src={photo.preview} alt={`Preview ${index}`} />
            <div className="photo-actions">
              <button onClick={() => handleDelete(index)}>Delete</button>
              {index > 0 && (
                <button onClick={() => handleReorder(index, index - 1)}>Move Up</button>
              )}
              {index < photos.length - 1 && (
                <button onClick={() => handleReorder(index, index + 1)}>Move Down</button>
              )}
            </div>
            {photo.uploading && (
              <progress value={uploadProgress[index] || 0} max={100}>
                {uploadProgress[index]}%
              </progress>
            )}
          </div>
        ))}
      </div>
      <button onClick={handleUpload} disabled={photos.length === 0}>
        Upload Photos
      </button>
    </div>
  );
};

export default PhotoUploader;