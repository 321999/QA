// .menu{
//   /* border:1px solid green; */
//   display:flex;
//   flex-direction:column;
//   gap:6px;
//   width:21px;
//   /* justify-content:right; */
// }
//   .bar{
//     border:1px solid red;
//     width:22px;
    
//   }
// </style>
  
// </head>

export default function MenuButton() {
return (  
  <>
<style>
{`
.menu{
  /* border:1px solid green; */
  display:flex;
  flex-direction:column;
  gap:5px;
  width:21px;
  /* justify-content:right; */
}
.bar{
    border:1px solid white;
    width:22px;
  }
  `}
</style>
  <div class="menu">
    <div class="bar"></div>
    <div class="bar mid"></div>
    <div class="bar"></div>
  </div>
</>
)
}