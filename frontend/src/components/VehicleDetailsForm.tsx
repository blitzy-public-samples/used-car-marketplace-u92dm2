import React from 'react';
import { Formik, Form, Field } from 'formik';
import { VehicleListingSchema } from '@/schema/listing';
import { validateVehicleDetails } from '@/utils/validation';

interface VehicleDetailsFormProps {
  initialValues: VehicleListingSchema;
  onSubmit: (values: VehicleListingSchema) => void;
}

const VehicleDetailsForm: React.FC<VehicleDetailsFormProps> = ({ initialValues, onSubmit }) => {
  return (
    <Formik
      initialValues={initialValues}
      validate={validateVehicleDetails}
      onSubmit={(values, { setSubmitting }) => {
        onSubmit(values);
        setSubmitting(false);
      }}
    >
      {({ errors, touched, isSubmitting }) => (
        <Form>
          <div>
            <label htmlFor="make">Make</label>
            <Field type="text" name="make" />
            {errors.make && touched.make && <div>{errors.make}</div>}
          </div>

          <div>
            <label htmlFor="model">Model</label>
            <Field type="text" name="model" />
            {errors.model && touched.model && <div>{errors.model}</div>}
          </div>

          <div>
            <label htmlFor="year">Year</label>
            <Field type="number" name="year" />
            {errors.year && touched.year && <div>{errors.year}</div>}
          </div>

          <div>
            <label htmlFor="mileage">Mileage</label>
            <Field type="number" name="mileage" />
            {errors.mileage && touched.mileage && <div>{errors.mileage}</div>}
          </div>

          <div>
            <label htmlFor="price">Price</label>
            <Field type="number" name="price" />
            {errors.price && touched.price && <div>{errors.price}</div>}
          </div>

          <div>
            <label htmlFor="description">Description</label>
            <Field as="textarea" name="description" />
            {errors.description && touched.description && <div>{errors.description}</div>}
          </div>

          <button type="submit" disabled={isSubmitting}>
            Submit
          </button>
        </Form>
      )}
    </Formik>
  );
};

export default VehicleDetailsForm;