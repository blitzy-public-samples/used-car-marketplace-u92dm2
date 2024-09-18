import axios, { AxiosInstance } from 'axios';
import { getAuthToken } from 'app/utils/auth';

const API_BASE_URL = process.env.REACT_APP_API_BASE_URL;

const createApiInstance = (): AxiosInstance => {
  const instance = axios.create({
    baseURL: API_BASE_URL,
  });

  instance.interceptors.request.use(async (config) => {
    const token = await getAuthToken();
    if (token) {
      config.headers['Authorization'] = `Bearer ${token}`;
    }
    return config;
  });

  instance.interceptors.response.use(
    (response) => response,
    (error) => {
      // HUMAN ASSISTANCE NEEDED
      // Add more specific error handling based on your application's requirements
      console.error('API request failed:', error);
      return Promise.reject(error);
    }
  );

  return instance;
};

export const fetchListings = async (filters: object): Promise<VehicleListing[]> => {
  const api = createApiInstance();
  const response = await api.get('/listings', { params: filters });
  return response.data;
};

export const createListing = async (listingData: VehicleListing): Promise<VehicleListing> => {
  const api = createApiInstance();
  const response = await api.post('/listings', listingData);
  return response.data;
};

export const uploadPhoto = async (photo: File): Promise<string> => {
  const api = createApiInstance();
  const formData = new FormData();
  formData.append('photo', photo);
  const response = await api.post('/upload', formData, {
    headers: {
      'Content-Type': 'multipart/form-data',
    },
  });
  return response.data;
};

// HUMAN ASSISTANCE NEEDED
// Define the VehicleListing interface based on your application's data model
interface VehicleListing {
  // Add properties for the vehicle listing
}