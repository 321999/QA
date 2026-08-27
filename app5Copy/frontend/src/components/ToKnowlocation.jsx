import {uselocation} from 'react-router-dom';

export default function ToKnowlocation() {
  const location = uselocation();
  console.log(location);
  return (
    <div>
      <h1>Current Location</h1> 
      <p>{location.pathname}</p>
    </div>
  );
}