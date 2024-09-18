import React, { useState } from 'react';
import { useDropzone } from 'react-dropzone';
import { uploadDocument } from '@/services/api';
import { processMaintenanceDocument } from '@/services/document_processing';

// HUMAN ASSISTANCE NEEDED
// This component may need additional styling and error handling for production readiness.
// Consider adding more robust error handling and user feedback mechanisms.

interface MaintenanceDocumentUploaderProps {
  onDocumentsUploaded: (documents: ProcessedDocument[]) => void;
}

interface ProcessedDocument {
  id: string;
  name: string;
  url: string;
  processedInfo: any; // Replace 'any' with a more specific type if available
}

const MaintenanceDocumentUploader: React.FC<MaintenanceDocumentUploaderProps> = ({ onDocumentsUploaded }) => {
  const [uploadedDocuments, setUploadedDocuments] = useState<ProcessedDocument[]>([]);
  const [uploadProgress, setUploadProgress] = useState<{ [key: string]: number }>({});

  const onDrop = async (acceptedFiles: File[]) => {
    for (const file of acceptedFiles) {
      try {
        // Upload document
        const uploadResult = await uploadDocument(file, (progress) => {
          setUploadProgress((prev) => ({ ...prev, [file.name]: progress }));
        });

        // Process document
        const processedInfo = await processMaintenanceDocument(uploadResult.url);

        // Add to uploaded documents
        const newDocument: ProcessedDocument = {
          id: uploadResult.id,
          name: file.name,
          url: uploadResult.url,
          processedInfo,
        };

        setUploadedDocuments((prev) => [...prev, newDocument]);
      } catch (error) {
        console.error(`Error uploading file ${file.name}:`, error);
        // TODO: Add user-friendly error handling
      }
    }

    // Call the callback with all uploaded documents
    onDocumentsUploaded(uploadedDocuments);
  };

  const { getRootProps, getInputProps, isDragActive } = useDropzone({ onDrop });

  const handleDelete = (id: string) => {
    setUploadedDocuments((prev) => prev.filter((doc) => doc.id !== id));
    // TODO: Implement server-side document deletion if necessary
  };

  return (
    <div>
      <div {...getRootProps()} style={{ border: '2px dashed #ccc', padding: '20px', textAlign: 'center' }}>
        <input {...getInputProps()} />
        {isDragActive ? (
          <p>Drop the files here ...</p>
        ) : (
          <p>Drag 'n' drop some files here, or click to select files</p>
        )}
      </div>
      <div>
        {uploadedDocuments.map((doc) => (
          <div key={doc.id}>
            <span>{doc.name}</span>
            <button onClick={() => handleDelete(doc.id)}>Delete</button>
            <div>Upload progress: {uploadProgress[doc.name] || 0}%</div>
          </div>
        ))}
      </div>
    </div>
  );
};

export default MaintenanceDocumentUploader;